"""Source evidence for observed PLCopen states, scoped to actual control writes."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import re

from lxml import etree

from plc_trace.analysis.atomic_boundaries import stable_id
from plc_trace.analysis.boundary_logic import constrained_states as _states
from plc_trace.analysis.boundary_logic import (IDENT, FALSE, TRUE, BoundarySyntaxError, Expr, combine,
                            comparisons, compatible, integer, negate, substitute, symbol)
from plc_trace.analysis.fb_transport import bindings, bound_expr, interface, required_signals
from plc_trace.source.source_expressions import (DECIMAL, INTEGER_BOUNDS, INTEGER_OPERATORS, combine_wiring,
                                required_atoms, source_condition, state_write_value)
from plc_trace.analysis.successor_graph import STParser, WireBody, WireError, connections, own_expr, owner, pin, pins, tag, text

ERRORS = (WireError, BoundarySyntaxError)
_COMPARATORS = {'EQ': '=', 'NE': '<>', 'GE': '>=', 'GT': '>', 'LE': '<=', 'LT': '<',
                '=': '=', '==': '=', '<>': '<>', '!=': '<>', '>=': '>=', '>': '>', '<=': '<=', '<': '<'}
_TIMER_OUTPUTS = {'TON', 'TOF', 'TP', 'UP', 'DOWN', 'R_TRIG', 'F_TRIG'}
_UNKNOWN = '@wired_output('
_ST_UNKNOWN = '@condition('
_ASSIGN = re.compile(rf'({IDENT})\s*:=')
_NAME = re.compile(IDENT)
_QUOTED = re.compile(r'''"(?:""|[^"])*"|'(?:''|[^'])*' ''', re.X)
_COMPARE_READ = re.compile(rf'({IDENT})\s*(?:<>|<=|>=|=|<|>)')
_STEP_LINE = re.compile(r'^\s*(?:#\s*|步\s*)?(\d+)(?:\s*[-—:：>]\s*|\s+)(?=\S)')
_MEMBER = re.compile(r'([^\W\d]\w*)((?:\[[^\[\]]+\])*)')
_COMPARISON_TEXT = {'eq': '=', 'veq': '=', 'ne': '≠', 'vne': '≠',
                    'lt': '<', 'le': '≤', 'gt': '>', 'ge': '≥'}
_ARITHMETIC_BLOCKS = {operator: operator for operator in INTEGER_OPERATORS} | {
    'ADD': '+', 'SUB': '-', 'MUL': '*',
}


def _root(name):
    return re.split(r'[.\[]', name, maxsplit=1)[0].casefold()


def _prefix(name, prefix):
    return name == prefix or name.startswith((prefix + '.', prefix + '['))


def _scope(unit):
    # Some exports store actions outside their POU. Preserve the vendor's
    # owner path when available, including similarly named actions in other POUs.
    path = unit.xpath('./*[local-name()="addData"]/*/*[local-name()="pathStructure"]')
    if len(path) == 1:
        names = [item.get('name') for item in path[0].iter()
                 if tag(item) == 'pathStructure' and item.get('name')]
        if names and names[-1] == unit.get('name'):
            return '::'.join(names)
    parent, _ = owner(unit)
    name = unit.get('name', '')
    return name if parent == '<anonymous>' else f'{parent}::{name}'


def comment_text(raw):
    value = raw.strip().strip('*').strip()
    if (not any(ch.isalpha() for ch in value)
            or value.casefold() in {'网络注释', '网络标题', '注释', 'network comment', 'network title', 'comment'}):
        return ''
    return value


def step_comment_text(raw, step, *, single_step=False, require_numbered=False):
    """Select an original clause from a numbered or single-step network note."""
    value = comment_text(raw)
    lines = value.splitlines()
    numbered = [(i, int(match[1])) for i, line in enumerate(lines)
                if (match := _STEP_LINE.match(line))]
    if not numbered:
        return '' if require_numbered else value
    matches = [lines[i].strip() for i, number in numbered if number == step]
    if len(set(matches)) == 1:
        if len(numbered) == 1 and numbered[0][0] == 0:
            return value
        return matches[0]
    first, _ = numbered[0]
    if not matches and single_step and len(numbered) == 1 and first:
        return comment_text('\n'.join(lines[:first]))
    return ''


def _program(unit, scope):
    if tag(unit) == 'pou':
        return scope
    pou = next((node for node in unit.iterancestors() if tag(node) == 'pou'), None)
    return _scope(pou) if pou is not None else (scope.rpartition('::')[0] or scope)


class StateTypes:
    """Resolve PLCopen declarations for integer expressions without type guesses."""

    def __init__(self, root, pous):
        self.aliases = defaultdict(list)
        self.globals = defaultdict(list)
        self.locals = {}
        self.cache = {}
        for item in root.iter('{*}dataType'):
            base = item.find('{*}baseType')
            if item.get('name') and base is not None:
                self.aliases[item.get('name').casefold()].extend(base)
        for group in root.iter('{*}globalVars'):
            # A vendor metadata copy nested inside the declaring group does
            # not declare a second variable.
            if any(tag(parent) == 'globalVars' for parent in group.iterancestors()):
                continue
            for name, types in self._declarations(group).items():
                self.globals[name].extend(types)
        for pou in pous:
            scope = _scope(pou)
            local = self.locals.setdefault(scope, defaultdict(list))
            for group in pou.findall('{*}interface/*'):
                if tag(group) != 'externalVars':
                    for name, types in self._declarations(group).items():
                        local[name].extend(types)

    @staticmethod
    def _declarations(group):
        result = defaultdict(list)
        for variable in group.findall('{*}variable'):
            box = variable.find('{*}type')
            if variable.get('name') and box is not None:
                result[variable.get('name').casefold()].extend(box)
        return result

    def bounds(self, variable, scope):
        key = (variable, scope)
        if key not in self.cache:
            self.cache[key] = self._bounds(variable, scope)
        return self.cache[key]

    def _unaliased(self, nodes):
        visited = set()
        while len(nodes) == 1 and tag(nodes[0]) == 'derived':
            name = nodes[0].get('name', '').casefold()
            if name in visited:
                return None
            visited.add(name)
            nodes = self.aliases.get(name, ())
        return nodes[0] if len(nodes) == 1 else None

    def _bounds(self, variable, scope):
        fields = [_MEMBER.fullmatch(part) for part in variable.split('.')]
        if not all(fields):
            return None
        name = fields[0][1]
        nodes = self.locals.get(scope, {}).get(name, self.globals.get(name, ()))
        for index, field in enumerate(fields):
            typ = self._unaliased(nodes)
            if field[2]:
                if typ is None or tag(typ) != 'array':
                    return None
                base = typ.find('{*}baseType')
                typ = self._unaliased(list(base) if base is not None else ())
            if typ is None:
                return None
            if index == len(fields) - 1:
                return INTEGER_BOUNDS.get(tag(typ).upper())
            if tag(typ) != 'struct':
                return None
            nodes = self._declarations(typ).get(fields[index + 1][1], ())


def _networks(body):
    """Locate a vendor network title and its attached free comment."""
    network, raw, note_line, pending = '', '', 0, None
    for node in body:
        kind = tag(node)
        if kind == 'comment':
            pending = node
            continue
        if kind == 'vendorElement' and any(
                tag(child) == 'ElementType' and text(child).casefold() == 'networktitle'
                for child in node.iter()):
            network = node.get('localId') or str(node.sourceline)
            title = next((text(child) for child in node if tag(child) == 'alternativeText'), '')
            raw, note_line = (title, node.sourceline) if comment_text(title) else (
                (text(pending), pending.sourceline) if pending is not None else ('', 0))
        pending = None
        yield node, network, raw, note_line


def _marked_output(node, port, name):
    out = pin(node, 'outputVariables', name)
    if out is None:
        return False
    if port:
        return port.upper() == name
    return any(
        tag(child) == 'IsMainOutput' and text(child).casefold() == 'true'
        for child in out.iter())


class EvidenceWire(WireBody):
    """Keep explicit dependencies on outputs whose runtime semantics are open."""

    combine_guard = staticmethod(combine_wiring)
    parse_guard = staticmethod(source_condition)

    def __init__(self, body, templates, output_models):
        super().__init__(body)
        self.templates = templates
        self.output_models = output_models
        self.output_provenance = {}
        self.enable_outputs = {}

    def exit_sources(self, expression, state_writers):
        """State writes on whose ENO the command is wired, within this LD body and call."""
        if expression.kind == 'opaque_cmp':
            writer = state_writers.get(self.enable_outputs.get(expression.args[0]))
            return {writer} if writer else set()
        if expression.kind not in ('and', 'or'):
            return set()
        arms = [self.exit_sources(child, state_writers) for child in expression.args]
        # A parallel feed bypassing all state writes can energize the command independently.
        if expression.kind == 'or' and not all(arms):
            return set()
        return set().union(*arms)

    def _template_output(self, node, port, kind):
        options = self.templates.get(kind.casefold(), ())
        if len(options) != 1 or not node.get('instanceName'):
            return None
        unit, formals = options[0]
        names = [p.get('formalParameter', '').casefold()
                 for p in pins(node, 'outputVariables')
                 if _marked_output(node, port, p.get('formalParameter', '').upper())]
        if len(names) != 1:
            return None
        name = names[0]
        formal = formals.get(name, {})
        if not formal.get('bool') or formal['direction'] != 'outputVars':
            return None
        key = (unit, name)
        if key not in self.output_models:
            self.output_models[key] = _output_requirements(unit, name, formals)
        return unit, name, self.output_models[key]

    def _bound_output(self, node, gate, template, opaque):
        unit, name, (code, line, requirements) = template
        actuals = []
        for formal, positive in requirements:
            ports = [p for p in pins(node, 'inputVariables')
                     if p.get('formalParameter', '').casefold() == formal]
            if len(ports) != 1:
                continue
            try:
                actual = self.cp(ports[0])
            except ERRORS:
                continue
            condition = actual if positive else negate(actual)
            actuals.append({'formal': formal, 'positive': positive, 'guard': condition.text()})
            gate = combine_wiring('and', gate, condition)
        self.output_provenance[opaque] = {
            'type': unit.get('name'), 'instance': node.get('instanceName'),
            'node': node.get('localId'), 'source_line': node.sourceline,
            'formal_output': name, 'definition_line': line,
            'output_expression': code, 'required_inputs': actuals,
        }
        return gate

    def selector_ports(self, control, actuals):
        """Identity of a physical EQ output on necessary series wiring. A bypass breaks the proof."""
        ports, seen = [], set()
        refs = connections(control)
        while len(refs) == 1:
            ref, port = refs[0]
            if ref in seen or ref not in self.nodes:
                break
            seen.add(ref)
            node = self.nodes[ref]
            if tag(node) == 'contact':
                refs = connections(next((x for x in node if tag(x) == 'connectionPointIn'), None))
                continue
            if tag(node) != 'block' or port.upper() == 'ENO' or node.get('typeName', '').upper() not in ('EQ', '=', '=='):
                break
            data = [p for p in pins(node, 'inputVariables') if p.get('formalParameter', '').upper() != 'EN']
            if len(data) == 2:
                try:
                    left, right = (self.value(p) for p in data)
                    for address, literal in ((left, right), (right, left)):
                        if re.fullmatch(IDENT, address) and (number := integer(literal)) is not None:
                            ports.append((bound_expr(address, actuals), number, ref, port.upper()))
                except ERRORS:
                    pass
            refs = connections(pin(node, 'inputVariables', 'EN'))
        return tuple(ports)

    def dependencies(self, guard):
        raw = guard.text()
        return tuple(value for key, value in self.output_provenance.items() if key in raw)

    def resolve(self, ref, port=''):
        node = self.nodes.get(ref)
        if node is None or self.duplicate_ids:
            return super().resolve(ref, port)
        kind = node.get('typeName', '').upper() if tag(node) == 'block' else ''
        edge = node.get('edge', '').casefold() if tag(node) == 'contact' else ''
        edge = edge if edge not in {'', 'none'} else ''
        en = pin(node, 'inputVariables', 'EN')
        comparison = kind in _COMPARATORS and port.upper() != 'ENO'
        enable = en is not None and _marked_output(node, port, 'ENO')
        timer = kind in _TIMER_OUTPUTS and (_marked_output(node, port, 'Q') or (
            kind in {'UP', 'DOWN'} and not port))
        template = self._template_output(node, port, kind) if kind else None
        if not (comparison or enable or timer or edge or template):
            return super().resolve(ref, port)
        key = (ref, port.upper())
        if key in self.memo:
            return self.memo[key]
        if key in self.stack:
            raise WireError('control_cycle:' + ref)
        self.stack.add(key)
        self.seen.add(ref)
        try:
            gate = self.cp(en, TRUE)
            if edge:
                gate = self.cp(next((c for c in node if tag(c) == 'connectionPointIn'), None))
                name = own_expr(node)
                polarity = ' NOT' if node.get('negated', '').casefold() == 'true' else ''
                result = combine_wiring('and', gate, Expr('opaque_cmp', (
                    f'{_ST_UNKNOWN}{name}{polarity} {_UNKNOWN}{edge}#{ref}))',)))
            elif comparison:
                data = [self.value(p) for p in pins(node, 'inputVariables')
                        if p.get('formalParameter', '').upper() != 'EN']
                if len(data) != 2 or not all(data):
                    raise WireError('nonbinary_comparison:' + ref)
                condition = data[0] + _COMPARATORS[kind] + data[1]
                predicate = source_condition(condition)
                output = pin(node, 'outputVariables', port)
                if output is not None and output.get('negated', '').lower() == 'true':
                    predicate = negate(predicate)
                result = combine_wiring('and', gate, predicate)
            else:
                if timer:
                    inp = pin(node, 'inputVariables', 'IN')
                    if inp is None:
                        inp = pin(node, 'inputVariables', 'CLK')
                    if inp is None and kind in {'UP', 'DOWN'}:
                        inp = pin(node, 'inputVariables', '')
                    gate = combine_wiring('and', gate, self.cp(inp))
                # Preserve the input path as a candidate. The named output
                # (Q, edge pulse, or ENO) still has to be evaluated by the PLC.
                label = node.get('instanceName') or kind
                output = port.upper() or (template[1].upper() if template else ('Q' if timer else 'ENO'))
                opaque = f'{_UNKNOWN}{label}#{ref}.{output})'
                if output == 'ENO':
                    self.enable_outputs[opaque] = ref
                if template:
                    gate = self._bound_output(node, gate, template, opaque)
                result = combine_wiring('and', gate, Expr('opaque_cmp', (opaque,)))
                selected = pin(node, 'outputVariables', output)
                if selected is not None and selected.get('negated', '').lower() == 'true':
                    result = negate(result)
            if self.guard_size(result) > self.max_guard_nodes:
                raise WireError('guard_expression_budget_exceeded:' + ref)
            self.memo[key] = result
            return result
        finally:
            self.stack.remove(key)


def _output_requirements(unit, output, formals):
    """Find necessary input levels for an explicit Boolean output assignment."""
    unavailable = ('', 0, ())
    bodies = _bodies(unit)
    if len(bodies) != 1 or tag(bodies[0]) != 'ST':
        return unavailable
    code = text(bodies[0])
    parser = STParser(code, condition_parser=source_condition, strict=False, retain_source=True)
    try:
        parser.seq()
    except ERRORS:
        return unavailable
    if any(item['reason'] != 'st_expression_assignment_unmodeled_effects'
           for item in parser.skipped):
        return unavailable
    assignments = [(rhs, gate, offset) for lhs, rhs, gate, offset, _ in parser.statements
                   if lhs.casefold() == output]
    if len(assignments) != 1 or assignments[0][1] != TRUE:
        return unavailable
    rhs, _, offset = assignments[0]
    written = {_root(lhs) for lhs, _, _, _, _ in parser.statements}
    requirements = [(name, positive) for name, positive in sorted(required_signals(source_condition(rhs)))
                    if name not in written
                    and (formal := formals.get(name, {})).get('direction') == 'inputVars'
                    and formal.get('bool')]
    return rhs, bodies[0].sourceline + code.count('\n', 0, offset), tuple(requirements)


def _bind_code(code, actuals):
    if not actuals:
        return code
    parts, end = [], 0
    for match in _QUOTED.finditer(code):
        parts.extend((bound_expr(code[end:match.start()], actuals), match[0]))
        end = match.end()
    return ''.join([*parts, bound_expr(code[end:], actuals)])


def _bound_guard(guard, actuals):
    if not actuals or guard.kind == 'const':
        return guard
    if guard.kind in {'and', 'or', 'xor', 'not'}:
        parts = tuple(_bound_guard(arg, actuals) for arg in guard.args)
        if guard.kind == 'not':
            return negate(parts[0])
        if guard.kind in {'and', 'or'}:
            return combine(guard.kind, *parts)
        return Expr(guard.kind, parts)
    if guard.kind == 'opaque_cmp':
        raw, = guard.args
        return guard if raw.startswith(_UNKNOWN) else Expr(guard.kind, (_bind_code(raw, actuals),))
    return source_condition(bound_expr(guard.text(), actuals))


def _conditions(guard, state_variable):
    """Expose the read operands separately from the surrounding control text."""
    if guard.kind == 'const':
        return set()
    if guard.kind in {'and', 'or', 'xor', 'not'}:
        return set().union(*(_conditions(arg, state_variable) for arg in guard.args))
    raw = guard.text()
    if raw.startswith(_UNKNOWN):
        return set()
    if raw.startswith(_ST_UNKNOWN):
        raw = raw[len(_ST_UNKNOWN):-1]
    raw = re.sub(r'@wired_output\([^)]*\)', '', _QUOTED.sub('', raw))
    return {symbol(m[0]) for m in _NAME.finditer(raw)
            if symbol(m[0]).casefold() not in {state_variable, 'true', 'false', 'not', 'and', 'or', 'xor'}}


def _numeric_conditions(guard, state_variable):
    terms = defaultdict(list)
    for atom in required_atoms(guard, _COMPARISON_TEXT).values():
        name, value = atom.args
        if name.casefold() != state_variable and re.fullmatch(DECIMAL, str(value)):
            terms[name].append({'operator': _COMPARISON_TEXT[atom.kind], 'value': str(value)})
    return dict(terms)


@dataclass(frozen=True)
class Site:
    scope: str
    body: str
    network: str
    node: str
    line: int
    raw: str
    note_line: int
    chain: tuple
    st_line: int = 0
    note_st_line: int = 0
    program: str = ''
    output_dependencies: tuple = ()
    frames: tuple = ()
    offset: int = -1
    selector_ports: tuple = ()
    operating_guard: str = ''
    commit: tuple = ()
    selectors: tuple = ()

    def evidence(self, guard):
        condition = guard.text()
        return {'scope': self.scope, 'program': self.program, 'body': self.body, 'network': self.network,
                'node': self.node, 'source_line': self.line, 'st_line': self.st_line,
                'instance_path': self.chain, 'guard': condition,
                'guard_complete': _UNKNOWN not in condition and _ST_UNKNOWN not in condition,
                'output_dependencies': self.output_dependencies,
                'frames': self.frames, 'offset': self.offset, 'selector_ports': self.selector_ports,
                'operating_guard': self.operating_guard, 'commit': dict(self.commit), 'selectors': self.selectors,
                'comment': self.raw if comment_text(self.raw) else ''}


class ControlIndex:
    policy = 'plcopen_control'

    def __init__(self, wanted, templates, types, recover_st=None):
        self.recover_st = recover_st
        self.wanted = wanted
        self.templates = templates
        self.types = types
        self.output_models = {}
        self.edges = []
        self.external_writes = []
        self.state_writes = defaultdict(list)
        self.notes = defaultdict(list)
        self.commands = defaultdict(list)
        self.programs = defaultdict(set)
        self.issues = Counter()
        self.affected = defaultdict(set)
        self.blockers = defaultdict(list)
        self.partial = set()
        self.bindings_found = set()
        self.branch_guards = {}
        self.command_exits = {}

    def _issue(self, reason, targets, site=None, detail=''):
        self.issues[reason] += 1
        for name in targets:
            self.affected[name].add(reason)
            if site is not None and len(self.blockers[name]) < 12:
                self.blockers[name].append({
                    'scope': site.scope, 'network': site.network, 'node': site.node,
                    'source_line': site.line, 'st_line': site.st_line,
                    'reason': reason, 'detail': detail,
                })

    def _external_write(self, target, value, site, gate, matched):
        if not matched or any(name==target for name,_ in matched):
            return
        if any(symbol(m[0]).casefold()==target for m in re.finditer(IDENT,gate.text())):
            return
        try:destination=integer(value)
        except ValueError:return
        if destination is not None:
            self.external_writes.append((target,destination,{**site.evidence(gate),
                'controllers':sorted(matched),'kind':'external_state_write','write_value':value}))

    def _state_write(self, state, destination, value, site, gate, grade, branch=None):
        if destination == state[1]:
            return False
        item = {**site.evidence(gate), 'binding': site.scope if site.chain else '',
                'from': state[1], 'to': destination, 'grade': grade, 'write_value': value,
                'condition_symbols': sorted(_conditions(gate, state[0])),
                'numeric_conditions': _numeric_conditions(gate, state[0]),
                'path': ''}
        item['source_id'] = stable_id('TL', [state, item])
        self.state_writes[state].append(item)
        self.programs[state[0]].add(site.program)
        if branch:
            self.branch_guards[item['source_id']] = branch
        if destination is not None:
            self.edges.append((state[0], item))
        else:
            self._issue('dynamic_state_write', (state[0],), site, value)
        if not item['guard_complete']:
            self.issues['unresolved_guard'] += 1
            self.partial.add(state)
        if comment_text(site.raw):
            note = {'raw': site.raw, 'scope': site.scope, 'network': site.network,
                    'body': site.body,
                    'source_id': item['source_id'], 'source_line': site.note_line,
                    'st_line': site.note_st_line, 'origin': grade, 'instance_path': site.chain}
            if note not in self.notes[state]:
                self.notes[state].append(note)
        return item['source_id']

    def _write(self, state, target, value, site, gate, command, branch=None):
        evidence = site.evidence(gate)
        item = {**evidence, 'start': f'{state[0]}={state[1]}',
                'command': command, 'arguments': target, 'write_value': value,
                'role': 'source_write', 'kind': '', 'operation': '',
                'issue': '' if evidence['guard_complete'] else '控制输出或条件待核', 'path': ''}
        item['id'] = stable_id('TW', item)
        if item not in self.commands[state]:
            self.commands[state].append(item)
        if branch:
            self.branch_guards[item['id']] = branch
        return item['id']

    def compatible_write(self, state, command, edges):
        """Check physical LD enable paths and exclusive ST arms for this exit."""
        exits = self.command_exits.get(command['id'])
        if exits and edges and not any(edge['source_id'] in exits for edge in edges):
            return False
        source = self.branch_guards.get(command['id'])
        if source is None:
            return True
        fragment, condition = source
        branches = []
        for edge in edges:
            origin = self.branch_guards.get(edge['source_id'])
            if origin and origin[0] == fragment:
                branches.append(origin[1])
        return not branches or any(compatible(combine_wiring('and', condition, guard), *state, check_remaining=True)
                                   for guard in branches)

    def _st(self, code, site, outer, actuals, read_only):
        def target(lhs):
            return '' if _root(lhs) in read_only else bound_expr(lhs, actuals)

        # Only executable writes to a traced state admit this fragment. A
        # comparison copied into a logger, a display table or a timing flag
        # has no state writer and cannot contribute either notes or effects.
        candidates = {target(m[1]).casefold() for m in _ASSIGN.finditer(code)}
        candidates &= self.wanted
        if not candidates:
            return set()
        parser = STParser(code, condition_parser=source_condition, strict=False, retain_source=True)
        grade = 'plcopen_structured_st'
        try:
            parser.seq()
            def same_type(lhs, rhs):
                left = self.types.bounds(target(lhs).casefold(), site.program)
                return left is not None and left == self.types.bounds(target(rhs).casefold(), site.program)
            statements = parser.staged_writes(parser.statements, same_type)
        except ERRORS as exc:
            self._issue('unsupported_st', candidates, site, str(exc))
            if self.recover_st is None:
                return set()
            statements = self.recover_st(code, parser, exc)
            if not statements:
                return set()
            grade = 'plcopen_partial_st_body'

        entries = []
        states = set()
        previous = defaultdict(list)
        fragment = (site.body, site.node)
        for lhs, rhs, condition, offset, note in statements:
            write = target(lhs)
            if not write:
                continue
            condition = _bound_guard(condition, actuals)
            gate = combine_wiring('and', outer, condition)
            line = 1 + code.count('\n', 0, offset)
            frames = tuple((k, d, a, bound_expr(v, actuals) if v else '') for k, d, a, v in parser.frames.get(offset, ()))
            operating = combine_wiring('and', outer, _bound_guard(parser.operating_condition(offset, lhs), actuals))
            commit = getattr(parser, 'commit_sites', {}).get(offset, {})
            located = replace(site, st_line=line, node=f'{site.node}:ST:{line}', frames=frames, offset=offset,
                              operating_guard='' if operating == TRUE else operating.text(), commit=tuple(commit.items()),
                              selectors=parser.selector_decisions(offset, lhs))
            if note and comment_text(note[0]):
                located = replace(located, raw=note[0], note_line=site.line,
                                  note_st_line=1 + code.count('\n', 0, note[1]))
            matched = set(_states(gate, self.wanted))
            if write.casefold() in self.wanted:
                variable = write.casefold()
                value = _bind_code(rhs, actuals)
                bounds = self.types.bounds(variable, site.program)
                self._external_write(variable,value,located,gate,matched)
                for state in sorted(matched):
                    if state[0] != variable:
                        continue
                    # An earlier write on a compatible branch can change the
                    # value read by a self-relative assignment in the same ST.
                    destination = state_write_value(value, variable, state[1], bounds)
                    reads_state = any(symbol(match[0]).casefold() == variable for match in _NAME.finditer(value))
                    if reads_state and any(compatible(combine_wiring('and', gate, prior), *state, check_remaining=True)
                                           for prior in previous[variable]):
                        destination = None
                    if value.casefold() != variable and self._state_write(
                            state, destination, value, located, gate, grade,
                            (fragment, condition)):
                        states.add(state)
                previous[variable].append(gate)
            else:
                entries.append((write, rhs, located, gate, matched, condition))
        for write, rhs, located, gate, matched, condition in entries:
            value = _bind_code(rhs, actuals)
            # An observation assignment reports a step value. The guard of an
            # actual instruction is represented separately by the ST parser.
            if any(symbol(m[1]).casefold() in self.wanted
                   for m in _COMPARE_READ.finditer(_QUOTED.sub('', value))):
                continue
            for state in sorted(states & matched):
                self._write(state, write, value, located, gate, 'st_write', (fragment, condition))
        return states

    def body(self, body, scope, program, actuals=None, read_only=(), chain=(), outer=TRUE, active=()):
        actuals = actuals or {}
        body_id = f'{scope}/{tag(body)}@{body.sourceline}'
        if tag(body) == 'ST':
            self._st(text(body), Site(scope, body_id, '', 'ST', body.sourceline, '', 0, chain, program=program),
                     outer, actuals, read_only)
            return
        wire = EvidenceWire(body, self.templates, self.output_models)
        known = defaultdict(set)
        state_writers = defaultdict(dict)
        writes = []
        for node, network, raw, note_line in _networks(body):
            kind = tag(node)
            if kind not in {'block', 'coil'}:
                continue
            typ = node.get('typeName', '').upper()
            site = Site(scope, body_id, network or body_id, node.get('localId', ''),
                        node.sourceline, raw, note_line, chain, program=program)
            operator = _ARITHMETIC_BLOCKS.get(typ) if not node.get('instanceName') else None
            if not (typ in {'MOVE', 'EXECUTE'} or operator or kind == 'coil'):
                options = self.templates.get(typ.casefold(), ())
                if options:
                    self._call(node, wire, site, actuals, read_only, outer, active, options)
                continue
            targets = []
            if typ == 'MOVE' or operator:
                try:
                    targets = wire.targets(node)
                except ERRORS as exc:
                    self._issue('unresolved_write', (), site, str(exc))
                    continue
                if not targets:
                    continue
            control = pin(node, 'inputVariables', 'EN') if kind == 'block' else node
            try:
                default = TRUE if operator else None
                wired = wire.cp(control, default)
                gate = combine_wiring('and', outer, _bound_guard(wired, actuals))
                site = replace(site, output_dependencies=wire.dependencies(wired), selector_ports=wire.selector_ports(control, actuals))
            except ERRORS as exc:
                watched = {bound_expr(name, actuals).casefold() for name in targets} & self.wanted
                if typ == 'EXECUTE':
                    watched.update(bound_expr(match[1], actuals).casefold()
                                   for child in node.iter('{*}STCode')
                                   for match in _ASSIGN.finditer(child.text or ''))
                    watched &= self.wanted
                self._issue('unresolved_wiring', watched, site, str(exc))
                continue
            if typ == 'EXECUTE':
                for child in node.iter():
                    if tag(child) == 'STCode' and child.text:
                        known[site.network].update(self._st(child.text, site, gate, actuals, read_only))
                continue
            matched = set(_states(gate, self.wanted))
            if not matched:
                continue
            if kind == 'coil':
                storage = node.get('storage', '').casefold()
                if storage not in {'set', 'reset'} or node.get('negated', '').lower() == 'true':
                    continue
                targets = [own_expr(node)]
                value = 'TRUE' if storage == 'set' else 'FALSE'
                command = 'coil_' + storage
            else:
                data = [p for p in pins(node, 'inputVariables') if p.get('formalParameter', '').upper() != 'EN']
                try:
                    if operator and len(data) == 2:
                        left, right = (wire.value(item) for item in data)
                        value = f'({left}) {operator} ({right})'
                    else:
                        value = wire.value(data[0]) if len(data) == 1 and not operator else ''
                except ERRORS as exc:
                    self._issue('unresolved_write', (), site, str(exc))
                    value = ''
                command = 'expression_write' if operator else 'move_write'
            for name in targets:
                if not name or _root(name) in read_only:
                    continue
                target = bound_expr(name, actuals)
                var = target.casefold()
                bound_value = _bind_code(value, actuals)
                if var in self.wanted:
                    self._external_write(var,bound_value,site,gate,matched)
                    if bound_value.casefold() == var:
                        continue
                    bounds = self.types.bounds(var, program)
                    for state in sorted(matched):
                        if state[0] == var:
                            destination = state_write_value(bound_value, var, state[1], bounds)
                            grade = 'plcopen_wired_expression' if operator else 'plcopen_wired_move'
                            source_id = self._state_write(state, destination, bound_value, site, gate, grade)
                            if source_id:
                                state_writers[state][site.node] = source_id
                                known[site.network].add(state)
                elif value:
                    writes.append((site, gate, target, bound_value, matched, command, wired))
        # Only the executable state network admits local writes. An
        # independent comparison network that logs the same step is excluded.
        # A command can precede its exit feedback during the step's dwell.
        # Its LD gate and the exit gate need not hold at the same instant.
        for site, gate, target, value, matched, command, wired in writes:
            for state in sorted(known[site.network] & matched):
                ident = self._write(state, target, value, site, gate, command)
                if exits := wire.exit_sources(wired, state_writers[state]):
                    self.command_exits[ident] = exits

    def _call(self, node, wire, site, parent, read_only, outer, active, options):
        actuals, ambiguous = bindings(node, wire)
        actuals = {key: bound_expr(value, parent) for key, value in actuals.items()
                   if _root(value) not in read_only}
        linked = {key: {v for v in self.wanted if _prefix(v, value.casefold())}
                  for key, value in actuals.items() if re.fullmatch(IDENT, value)}
        targets = set().union(*linked.values())
        if not targets:
            return
        if len(options) != 1:
            self._issue('ambiguous_fb_type', targets)
            return
        unit, formals = options[0]
        writable = {key for key, param in formals.items() if param['direction'] == 'inOutVars'}
        state_targets = set().union(*(linked[key] for key in writable & linked.keys()))
        if not state_targets:
            return
        if unit in active or ambiguous & writable:
            self._issue('unresolved_fb_binding', state_targets)
            return
        try:
            en = pin(node, 'inputVariables', 'EN')
            gate = combine('and', outer, _bound_guard(wire.cp(en, TRUE), parent))
        except ERRORS as exc:
            self._issue('unresolved_fb_enable', state_targets, site, str(exc))
            return
        instance = node.get('instanceName')
        if not instance:
            self._issue('unresolved_fb_instance', state_targets)
            return
        alias = {key: value for key, value in actuals.items() if key in formals}
        ro = {key for key, param in formals.items()
              if param['direction'] != 'inOutVars' or key not in alias}
        call = {'scope': site.scope, 'node': site.node, 'source_line': site.line, 'body': site.body,
                'network': site.network, 'guard': gate.text(), 'selector_ports': wire.selector_ports(en, parent),
                'instance': instance, 'type': unit.get('name'),
                'actuals': alias,
                'formal_documentation': {key: formals[key]['documentation'] for key in alias
                                         if formals[key]['documentation']},
                'reference_formals': sorted(key for key in alias if key not in ro)}
        self.bindings_found.update(state_targets)
        scope = f'{site.scope}::{instance}@{site.line}'
        for body in _bodies(unit):
            self.body(body, scope, site.program, alias, ro, (*site.chain, call), gate, (*active, unit))


def _bodies(unit):
    return [body for box in unit if tag(box) == 'body' for body in box
            if tag(body) in {'ST', 'LD', 'FBD'}]


def collect(source, variables):
    """Index observed variables from PLCopen source and explicitly bound FBs."""
    root = etree.parse(str(source), etree.XMLParser(
        huge_tree=True, resolve_entities=False, no_network=True)).getroot()
    return collect_root(root, variables)


def collect_root(root, variables, *, recover_st=None):
    """Index controls on an already parsed project, including concrete FB actuals."""
    wanted = {symbol(name).casefold() for name in variables if re.fullmatch(IDENT, name)}
    templates = defaultdict(list)
    units = []
    for unit in root.iter('{*}pou', '{*}action', '{*}method'):
        if tag(unit) == 'pou' and unit.get('pouType', '').casefold() == 'functionblock':
            templates[unit.get('name', '').casefold()].append((unit, interface(unit)))
        elif not any(a.get('pouType', '').casefold() == 'functionblock' for a in unit.iterancestors()):
            units.append(unit)
    result = ControlIndex(wanted, templates, StateTypes(root, root.iter('{*}pou')), recover_st)
    if not wanted:
        return result
    roots = '|'.join(re.escape(value) for value in sorted({_root(v) for v in wanted}))
    relevant = re.compile(rf'(?<![\w.])(?:{roots})(?!\w)', re.I)
    for unit in units:
        for body in _bodies(unit):
            nodes = body.iter('{*}expression', '{*}variable', '{*}STCode')
            if (relevant.search(text(body)) if tag(body) == 'ST'
                    else any(relevant.search(node.text or '') for node in nodes)):
                scope = _scope(unit)
                result.body(body, scope, _program(unit, scope))
    return result
