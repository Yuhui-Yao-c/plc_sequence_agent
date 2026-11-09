"""Source evidence for observed PLCopen states, scoped to actual control writes."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import re

from lxml import etree

from atomic_boundaries import stable_id
from boundary_logic import (IDENT, FALSE, TRUE, BoundarySyntaxError, Expr, combine,
                            comparisons, compatible, integer, negate, parse, substitute, symbol)
from fb_transport import bindings, bound_expr, interface
from successor_graph import STParser, WireBody, WireError, own_expr, owner, pin, pins, tag, text

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
        if not (comparison or enable or timer or edge):
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
                result = combine('and', gate, Expr('opaque_cmp', (
                    f'{_ST_UNKNOWN}{name}{polarity} {_UNKNOWN}{edge}#{ref}))',)))
            elif comparison:
                data = [self.value(p) for p in pins(node, 'inputVariables')
                        if p.get('formalParameter', '').upper() != 'EN']
                if len(data) != 2 or not all(data):
                    raise WireError('nonbinary_comparison:' + ref)
                condition = data[0] + _COMPARATORS[kind] + data[1]
                predicate = _condition(condition)
                output = pin(node, 'outputVariables', port)
                if output is not None and output.get('negated', '').lower() == 'true':
                    predicate = negate(predicate)
                result = combine('and', gate, predicate)
            else:
                if timer:
                    inp = pin(node, 'inputVariables', 'IN')
                    if inp is None:
                        inp = pin(node, 'inputVariables', 'CLK')
                    if inp is None and kind in {'UP', 'DOWN'}:
                        inp = pin(node, 'inputVariables', '')
                    gate = combine('and', gate, self.cp(inp))
                # Preserve the input path as a candidate. The named output
                # (Q, edge pulse, or ENO) still has to be evaluated by the PLC.
                label = node.get('instanceName') or kind
                output = port.upper() or ('Q' if timer else 'ENO')
                result = combine('and', gate, Expr('opaque_cmp', (
                    f'{_UNKNOWN}{label}#{ref}.{output})',)))
            if self.guard_size(result) > self.max_guard_nodes:
                raise WireError('guard_expression_budget_exceeded:' + ref)
            self.memo[key] = result
            return result
        finally:
            self.stack.remove(key)


def _condition(expression):
    try:
        return parse(expression)
    except BoundarySyntaxError:
        return Expr('opaque_cmp', (f'{_ST_UNKNOWN}{expression})',))


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
    return _condition(bound_expr(guard.text(), actuals))


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


def _states(guard, watched):
    anchors = defaultdict(set)
    atoms = comparisons(guard)
    for atom in atoms:
        name, number = atom.args
        if atom.kind == 'eq' and name.casefold() in watched:
            anchors[name.casefold()].add(number)
    for name, values in anchors.items():
        # A branch that can also run outside these equality values provides
        # no unambiguous start state. In particular, preserve OR bypasses.
        other = max(a.args[1] for a in atoms if a.args[0].casefold() == name) + 1
        if substitute(guard, name, other) != FALSE:
            continue
        for value in sorted(values):
            if compatible(guard, name, value):
                yield name, value


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

    def evidence(self, guard):
        condition = guard.text()
        return {'scope': self.scope, 'body': self.body, 'network': self.network,
                'node': self.node, 'source_line': self.line, 'st_line': self.st_line,
                'instance_path': self.chain, 'guard': condition,
                'guard_complete': _UNKNOWN not in condition and _ST_UNKNOWN not in condition,
                'comment': self.raw if comment_text(self.raw) else ''}


class ControlIndex:
    policy = 'plcopen_control'

    def __init__(self, wanted, templates):
        self.wanted = wanted
        self.templates = templates
        self.edges = []
        self.notes = defaultdict(list)
        self.commands = defaultdict(list)
        self.issues = Counter()
        self.affected = defaultdict(set)
        self.partial = set()
        self.bindings_found = set()
        self.st_guards = {}

    def _issue(self, reason, targets):
        self.issues[reason] += 1
        for name in targets:
            self.affected[name].add(reason)

    def _edge(self, state, destination, site, gate, grade):
        if destination == state[1]:
            return
        item = {**site.evidence(gate), 'binding': site.scope if site.chain else '',
                'from': state[1], 'to': destination, 'grade': grade,
                'condition_symbols': sorted(_conditions(gate, state[0])),
                'path': ''}
        item['source_id'] = stable_id('TL', [state, item])
        self.edges.append((state[0], item))
        if not item['guard_complete']:
            self.issues['unresolved_guard'] += 1
            self.partial.add(state)
        if comment_text(site.raw):
            note = {'raw': site.raw, 'scope': site.scope, 'network': site.network,
                    'source_id': item['source_id'], 'source_line': site.note_line,
                    'st_line': site.note_st_line, 'origin': grade, 'instance_path': site.chain}
            if note not in self.notes[state]:
                self.notes[state].append(note)
        return item['source_id']

    def _write(self, state, target, value, site, gate, command):
        evidence = site.evidence(gate)
        item = {**evidence, 'start': f'{state[0]}={state[1]}',
                'command': command, 'arguments': target, 'write_value': value,
                'role': 'source_write', 'kind': '', 'operation': '',
                'issue': '' if evidence['guard_complete'] else '控制输出或条件待核', 'path': ''}
        item['id'] = stable_id('TW', item)
        if item not in self.commands[state]:
            self.commands[state].append(item)
        return item['id']

    def compatible_write(self, state, command, edges):
        """Keep opposite arms of the same ST execution separate."""
        source = self.st_guards.get(command['id'])
        if source is None:
            return True
        fragment, condition = source
        branches = []
        for edge in edges:
            origin = self.st_guards.get(edge['source_id'])
            if origin and origin[0] == fragment:
                branches.append(origin[1])
        return not branches or any(compatible(combine('and', condition, guard), *state) for guard in branches)

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
        parser = STParser(code, condition_parser=_condition, strict=False, retain_source=True)
        try:
            parser.seq()
        except ERRORS:
            self._issue('unsupported_st', candidates)
            return set()

        entries = []
        states = set()
        fragment = (site.body, site.node)
        for lhs, rhs, condition, offset, note in parser.statements:
            write = target(lhs)
            if not write:
                continue
            condition = _bound_guard(condition, actuals)
            gate = combine('and', outer, condition)
            line = 1 + code.count('\n', 0, offset)
            located = replace(site, st_line=line, node=f'{site.node}:ST:{line}')
            if note and comment_text(note[0]):
                located = replace(located, raw=note[0], note_line=site.line,
                                  note_st_line=1 + code.count('\n', 0, note[1]))
            matched = set(_states(gate, self.wanted))
            if write.casefold() in self.wanted:
                destination = integer(rhs)
                if destination is None:
                    self._issue('dynamic_state_write', (write.casefold(),))
                    continue
                for state in sorted(matched):
                    if state[0] == write.casefold():
                        source_id = self._edge(state, destination, located, gate, 'plcopen_structured_st')
                        if source_id:
                            self.st_guards[source_id] = (fragment, condition)
                        states.add(state)
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
                source_id = self._write(state, write, value, located, gate, 'st_write')
                self.st_guards[source_id] = (fragment, condition)
        return states

    def body(self, body, scope, actuals=None, read_only=(), chain=(), outer=TRUE, active=()):
        actuals = actuals or {}
        body_id = f'{scope}/{tag(body)}@{body.sourceline}'
        if tag(body) == 'ST':
            self._st(text(body), Site(scope, body_id, '', 'ST', body.sourceline, '', 0, chain),
                     outer, actuals, read_only)
            return
        wire = EvidenceWire(body)
        known = defaultdict(set)
        writes = []
        for node, network, raw, note_line in _networks(body):
            kind = tag(node)
            if kind not in {'block', 'coil'}:
                continue
            typ = node.get('typeName', '').upper()
            site = Site(scope, body_id, network or body_id, node.get('localId', ''),
                        node.sourceline, raw, note_line, chain)
            if typ not in {'MOVE', 'EXECUTE'} and kind != 'coil':
                options = self.templates.get(typ.casefold(), ())
                if options:
                    self._call(node, wire, site, actuals, read_only, outer, active, options)
                continue
            control = pin(node, 'inputVariables', 'EN') if kind == 'block' else node
            try:
                gate = combine('and', outer, _bound_guard(wire.cp(control), actuals))
            except ERRORS:
                self.issues['unresolved_wiring'] += 1
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
                    targets = wire.targets(node)
                    value = wire.value(data[0]) if len(data) == 1 else ''
                except ERRORS:
                    self.issues['unresolved_write'] += 1
                    continue
                command = 'move_write'
            for name in targets:
                if not name or _root(name) in read_only:
                    continue
                target = bound_expr(name, actuals)
                var = target.casefold()
                if var in self.wanted:
                    destination = integer(value)
                    if destination is None:
                        self._issue('dynamic_state_write', (var,))
                        continue
                    for state in sorted(matched):
                        if state[0] == var:
                            self._edge(state, destination, site, gate, 'plcopen_wired_move')
                            known[site.network].add(state)
                elif value:
                    writes.append((site, gate, target, _bind_code(value, actuals), matched, command))
        # Only the executable state network admits local writes. An
        # independent comparison network that logs the same step is excluded.
        for site, gate, target, value, matched, command in writes:
            for state in sorted(known[site.network] & matched):
                self._write(state, target, value, site, gate, command)

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
        except ERRORS:
            self._issue('unresolved_fb_enable', state_targets)
            return
        instance = node.get('instanceName')
        if not instance:
            self._issue('unresolved_fb_instance', state_targets)
            return
        alias = {key: value for key, value in actuals.items() if key in formals}
        ro = {key for key, param in formals.items()
              if param['direction'] != 'inOutVars' or key not in alias}
        call = {'scope': site.scope, 'node': site.node, 'source_line': site.line,
                'instance': instance, 'type': unit.get('name'),
                'actuals': alias,
                'formal_documentation': {key: formals[key]['documentation'] for key in alias
                                         if formals[key]['documentation']},
                'reference_formals': sorted(key for key in alias if key not in ro)}
        self.bindings_found.update(state_targets)
        scope = f'{site.scope}::{instance}@{site.line}'
        for body in _bodies(unit):
            self.body(body, scope, alias, ro, (*site.chain, call), gate, (*active, unit))


def _bodies(unit):
    return [body for box in unit if tag(box) == 'body' for body in box
            if tag(body) in {'ST', 'LD', 'FBD'}]


def collect(source, variables):
    """Index observed variables from PLCopen source and explicitly bound FBs."""
    wanted = {symbol(name).casefold() for name in variables if re.fullmatch(IDENT, name)}
    root = etree.parse(str(source), etree.XMLParser(
        huge_tree=True, resolve_entities=False, no_network=True)).getroot()
    templates = defaultdict(list)
    units = []
    for unit in root.iter('{*}pou', '{*}action', '{*}method'):
        if tag(unit) == 'pou' and unit.get('pouType', '').casefold() == 'functionblock':
            templates[unit.get('name', '').casefold()].append((unit, interface(unit)))
        elif not any(a.get('pouType', '').casefold() == 'functionblock' for a in unit.iterancestors()):
            units.append(unit)
    result = ControlIndex(wanted, templates)
    if not wanted:
        return result
    roots = '|'.join(re.escape(value) for value in sorted({_root(v) for v in wanted}))
    relevant = re.compile(rf'(?<![\w.])(?:{roots})(?!\w)', re.I)
    for unit in units:
        for body in _bodies(unit):
            nodes = body.iter('{*}expression', '{*}variable', '{*}STCode')
            if (relevant.search(text(body)) if tag(body) == 'ST'
                    else any(relevant.search(node.text or '') for node in nodes)):
                result.body(body, _scope(unit))
    return result
