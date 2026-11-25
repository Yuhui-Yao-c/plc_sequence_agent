"""Resolve Sysmac state machines in program scope and through typed writable FB calls."""
import re
from collections import Counter, defaultdict
from functools import lru_cache

from plc_trace.analysis.boundary_logic import FALSE, IDENT, TRUE, combine, comparisons, integer, symbol
from plc_trace.analysis.successor_graph import STParser, _st_tokens_with_spans
from plc_trace.capture.extract_step_variables import INSTANCE_STORAGE, _executable_text
from plc_trace.capture.step_discovery import Discovery, fixed_path, isolate_counted_loops
from plc_trace.capture.station_scope import required_cursors
from plc_trace.source.omron_native import collect_native
from plc_trace.source.plcopen_trace_control import ERRORS, _bound_guard, _conditions, _states, step_comment_text
from plc_trace.source.smc2_controlflow import collect_smc2
from plc_trace.source.source_expressions import INTEGER_BOUNDS, source_condition, state_write_value


_CALL = re.compile(r'(?<![\w.#])([^\W\d]\w*(?:\s*\[[^\[\]]+\])?)\s*\(')
_PIN = re.compile(rf'([^\W\d]\w*)\s*(:=|=>)\s*({IDENT})')


def _root(path):
    return re.split(r'[.\[]', path, 1)[0].casefold()


def _args(code, start):
    closing = [')']
    args = []
    for token in _PUNCT.finditer(code, start):
        mark = token[0]
        if mark in '([':
            closing.append(')' if mark == '(' else ']')
        elif mark in ')]':
            if closing.pop() != mark:
                return []
            if not closing:
                return [*args, code[start:token.start()].strip()]
        elif len(closing) == 1:
            args.append(code[start:token.start()].strip())
            start = token.end()
    return []


_PUNCT = re.compile(r'[()\[\],]')


class Bindings:
    def __init__(self, scan, slices, wired_calls=()):
        self.scan = scan
        self.incoming = defaultdict(list)
        self.limits = Counter()
        self.unbound = set()
        self.instance_members = defaultdict(dict)
        for (unit, name), directions in scan.directions.items():
            if len(directions) == 1 and directions <= INSTANCE_STORAGE:
                declared = scan.declaration_sites.get(('variable', unit, name))
                if declared:
                    self.instance_members[unit][name] = declared[0]['name']
        for site in wired_calls:
            caller = self.unit(site['scope'])
            typ = self.type_of(site['instance'], caller)
            if caller and (typ or '').casefold() == site['type'].casefold() and fixed_path(site['instance']):
                self._register(caller, self.unit(typ), site['pins'], {k:v for k,v in site.items() if k != 'pins'})
        for block in slices:
            caller = self.unit(block['scope'])
            if not caller:
                continue
            code = ''.join(_executable_text(block['code'], keep_positions=True))
            gates = None
            for match in _CALL.finditer(code):
                instance = symbol(match[1])
                if not fixed_path(instance):
                    continue
                typ = self.type_of(instance, caller)
                options = scan.units.get((typ or '').casefold(), set())
                if len(options) != 1 or (typ or '').casefold() in scan.ambiguous_units:
                    continue
                callee, = options
                if callee[0] != 'functionblock':
                    continue
                pins = [(m[1], symbol(m[3])) for arg in _args(code, match.end())
                        if (m := _PIN.fullmatch(arg)) and m[2] == ':=']
                if gates is None:
                    parser = STParser(block['code'], condition_parser=source_condition, strict=False, retain_source=True)
                    try:
                        parser.seq()
                    except ERRORS:
                        pass  # Only call sites actually reached by structured parsing have a known enable.
                    gates = parser.call_guards
                inner = gates.get(match.start())
                if inner is None:
                    self.unbound.add((block['scope'], instance, 'unresolved_call_control'))
                    continue
                complete = not block.get('external_issue') and not block.get('symbolic_external')
                gate = combine('and', source_condition(block['external_guard']), inner)
                self._register(caller, callee, pins, {'scope': block['scope'], 'instance': instance,
                    'node': block['node'], 'network': block['network'], 'offset': match.start(),
                    'guard': gate.text(), 'guard_complete': complete,
                    'type': callee[1], 'source_line': block['source_line']})

    def _register(self, caller, callee, pins, site):
        if not callee or callee[0] != 'functionblock' or source_condition(site['guard']) == FALSE:
            return
        mapping = {}
        counts = Counter(formal.casefold() for formal, _ in pins)
        for formal, actual in pins:
            key = formal.casefold()
            if (counts[key] == 1 and self.scan.directions.get((callee, key)) == {'inoutvars'}
                    and fixed_path(actual) and self.type_of(actual, caller)
                    and self.type_of(actual, caller) == self.type_of(formal, callee)):
                mapping[key] = actual
        self.incoming[callee].append((caller, {**site, 'reference_actuals': mapping}))

    @lru_cache(maxsize=None)
    def members_at(self, typename, instance):
        return {key: f'{instance}.{name}' for key, name in self.instance_members[self.unit(typename)].items()}

    def unit(self, scope):
        options = self.scan.units.get(scope.split('::')[0].casefold(), set())
        return next(iter(options)) if len(options) == 1 and scope.split('::')[0].casefold() not in self.scan.ambiguous_units else None

    def type_of(self, name, unit):
        parts = name.split('.')
        root = _root(parts[0])
        declared = self.scan.declarations.get((unit, root)) or self.scan.declarations.get((None, root))
        kind, _ = self.scan._peel_type(declared or (), parts[0])
        for part in parts[1:]:
            if not kind:
                break
            kind, _ = self.scan._peel_type(self.scan.member(kind, _root(part))[0], part)
        return kind

    @lru_cache(maxsize=None)
    def paths(self, var, unit, active=()):
        if unit is None or unit in active:
            self.limits['unknown_or_recursive_scope'] += 1
            return []
        if unit[0] == 'program':
            name, kind, issue, _ = self.scan.sysmac_channel(var, unit)
            if issue and '.' in var:
                prefix, field = var.rsplit('.', 1)
                container = self.type_of(prefix, unit)
                if container and not self.scan.member(container, _root(field))[0]:
                    issue = f'缺少 {container}.{field} 的叶声明'
            return [(name, kind, issue, [])]
        root = _root(var)
        persistent = root in self.instance_members[unit]
        reference = self.scan.directions.get((unit, root)) == {'inoutvars'}
        global_address = (unit, root) not in self.scan.declarations and (None, root) in self.scan.declarations
        if unit[0] != 'functionblock' or not (persistent or reference or global_address):
            self.limits['template_local_or_nonreference_parameter'] += 1
            return []
        result = []
        for caller, call in self.incoming[unit]:
            if global_address:
                name, kind, issue, _ = self.scan.sysmac_channel(var, None)
                for _, _, _, chain in self.paths(call['instance'], caller, active + (unit,)):
                    result.append((name, kind, issue, [*chain, call]))
                continue
            elif persistent:
                actual = call['instance'] + '.' + var
            elif root in call['reference_actuals']:
                actual = call['reference_actuals'][root] + var[len(root):]
            else:
                continue
            for name, kind, issue, chain in self.paths(actual, caller, active + (unit,)):
                result.append((name, kind, issue, [*chain, call]))
        if not result:
            self.limits['no_bound_writable_instance'] += 1
            self.unbound.add((unit[1], var, 'no_bound_writable_instance'))
        return result


def _case_regions(code):
    stack, ranges = [], defaultdict(list)
    tokens = list(_st_tokens_with_spans(code))
    for idx, (word, span) in enumerate(tokens):
        if word.upper() == 'CASE':
            end = idx + 1
            while end < len(tokens) and tokens[end][0].upper() not in ('OF', ';', 'END_CASE'):
                end += 1
            selector = symbol(''.join(w for w, _ in tokens[idx+1:end]))
            stack.append((selector.casefold(), span[0]))
        elif word.upper() == 'END_CASE' and stack:
            name, start = stack.pop()
            ranges[name].append((start, span[1]))
    return ranges


def _numbered_network_notes(transitions):
    """A shared rung heading can identify a step only for one state variable."""
    owners = defaultdict(set)
    notes = {}
    for rel in transitions:
        raw = rel.get('network_comment')
        if not raw:
            continue
        key = (rel['scope'], rel['body'].split(';', 1)[0], rel['from'])
        owners[key].add(rel['variable'].casefold())
        note = step_comment_text(raw, rel['from'], require_numbered=True)
        if note:
            notes[rel['id']] = (key, note)
    return {ident: note for ident, (key, note) in notes.items() if len(owners[key]) == 1}


def discover(source, scan, *, on_transition=None, on_unresolved=None, on_entry=None):
    grid = scan.source_format == 'smc2'
    model = collect_smc2(source) if grid else collect_native(source, instance_types=scan.units.keys())
    slices = model.get('native_inline_slices', [])
    binding = Bindings(scan, slices, model.get('native_fb_calls', ()))
    result = Discovery()
    result.visible.update(name for name, _, _ in scan.references.values())
    result.visible.update(scan.dynamic.values())
    for (_, unit), (name, _, _) in scan.references.items():
        if unit and unit[0] == 'program' and (unit, _root(name)) in scan.declarations:
            result.visible.add(f'{unit[1]}.{name}')
    limits = Counter()

    def records(var, scope, site, co_names=()):
        paths = ([(f'{scope}::{var}', None, 'SMC2 中缺少可核实的作用域及整数声明', [])] if grid
                 else binding.paths(var, binding.unit(scope)))
        for name, kind, issue, chain in paths:
            row = result.candidate(name)
            row.declared, row.type_issue = kind, issue
            bound = {**site, 'scope': scope, **({'binding': chain} if chain else {})}
            if chain and 'guard' in site:
                gate = source_condition(site['guard'])
                operating = source_condition(site.get('operating_guard') or 'TRUE')
                for call in reversed(chain):
                    invoke = source_condition(call['guard'])
                    aliases = binding.members_at(call['type'], call['instance']) | call['reference_actuals']
                    gate = combine('and', invoke, _bound_guard(gate, aliases))
                    operating = combine('and', invoke, _bound_guard(operating, aliases))
                bound.update(guard=gate.text(), operating_guard='' if operating == TRUE else operating.text(),
                             guard_complete=site.get('guard_complete', False) and all(c['guard_complete'] for c in chain))
            host = binding.unit(chain[0]['scope'] if chain else scope)
            if host and host[0] == 'program':
                bound['program'] = host[1]
            if not grid and co_names:
                peers = set()
                for peer in co_names - {var.casefold()}:
                    for address, _, _, calls in binding.paths(peer, binding.unit(scope)):
                        if calls == chain and address.casefold() != name.casefold():
                            peers.add(address.casefold())
                bound['co_cursors'] = sorted(peers)
            yield row, bound

    # Either adapter may see the same ST instruction. Match its actual token within
    # the original XML fragment. Distinct instructions on one line keep separate IDs.
    native_st = {}
    network_notes = _numbered_network_notes(model['transitions']) if on_transition is not None and not grid else {}
    # Native weak connectivity can be enough for an audit but never proves an
    # input state when another arm bypasses its comparison.
    for rel in model['transitions']:
        if rel['from'] == rel['to']:
            continue
        var, scope = rel['variable'], rel['scope']
        if not fixed_path(var):
            limits['dynamic_transition_reference'] += 1
            continue
        condition = source_condition(rel['guard'])
        if (var.casefold(), rel['from']) not in _states(condition, {var.casefold()}):
            continue
        line = re.search(r'(?:^|;)line:(\d+)', rel['body'])
        site = {'source_line': int(line[1]) if line else 0, 'guard': rel['guard'],
                'guard_complete': rel.get('guard_complete', False), 'kind': rel['evidence']}
        for row, bound in records(var, scope, site, required_cursors(condition)):
            if on_transition is not None:
                network = re.match(r'network:(\d+)', rel['body']) if grid else None
                raw = bound['guard']
                predicates = model.get('source_predicates', {})
                dependencies=[]
                for token in re.findall(r'__source_predicate_[0-9a-f]+', raw):
                    if token in predicates:
                        if predicates[token] not in dependencies:dependencies.append(predicates[token])
                        raw = raw.replace(token, '(' + predicates[token]['expression'] + ')')
                fragment, _, node = rel['body'].partition(';line:')
                node = node.partition(';')[2]
                location = dict(bound, guard=raw, locator=rel['body'], network=fragment, node=node,
                                selector=var, selector_ports=rel.get('selector_ports', ()))
                if not grid and not any(str(p).startswith('token:') for p in rel['path']):
                    location['rung_order']=int(fragment) if fragment.isdigit() else None
                if dependencies:location['dependencies']=dependencies
                if rel['id'] in network_notes:
                    location['comment'] = network_notes[rel['id']]
                if network:
                    location['grid_network'] = int(network[1])
                token = next((p for p in rel['path'] if str(p).startswith('token:')), None)
                if token:
                    key = (row.name.casefold(), scope, fragment, node, int(token[6:]), rel['from'], rel['to'],
                           tuple((c['instance'], c['source_line'], c.get('node'), c.get('offset')) for c in location.get('binding', ())))
                    native_st[key] = (row.name, rel['from'], rel['to'], location)
                else:
                    on_transition(row.name, rel['from'], rel['to'], location)
            row.edge(rel['from'], rel['to'], bound)

    # Retain locations and non-monitor instructions in the same executable ST
    # branch. In an unsupported body accept only the parsed prefix before the
    # first unsupported control statement; never flatten or resume after it.
    for block in slices:
        if block.get('external_issue'):
            continue
        code = block['code']
        available, loop_writes = isolate_counted_loops(code) if block.get('issue', '').startswith('ST unsupported flow FOR') else (code, set())
        if loop_writes:
            limits['counted_loop_contents_omitted'] += 1
        parser = STParser(available, condition_parser=source_condition, strict=False, retain_source=True)
        try:
            parser.seq()
        except ERRORS as exc:
            limits[str(exc).split(':')[0][:100]] += 1
        original = list(_st_tokens_with_spans(code)) if on_transition is not None else []
        token_at = {span[0]: i for i, (_, span) in enumerate(original)}
        unsupported = {'FOR', 'WHILE', 'REPEAT', 'JMP', 'RETURN', 'EXIT', 'CONTINUE', 'GOTO'}
        intact_prefix = next((span[0] for word, span in original if word.upper() in unsupported), 0)
        regions = _case_regions(code)
        outer = source_condition(block['external_guard'])
        def external_writer(lhs,rhs,gate,scope_site):
            if not on_entry or not fixed_path(lhs):return
            try:number=integer(rhs)
            except ValueError:return
            if number is None or any(symbol(m[0]).casefold()==lhs.casefold() for m in re.finditer(IDENT,gate.text())):return
            controls=sorted(_states(gate,required_cursors(gate)))
            if controls:
                for row,bound in records(lhs,block['scope'],scope_site):
                    if row.declared and row.declared.upper() in INTEGER_BOUNDS:
                        on_entry(row.name,number,{**bound,'kind':'external_state_write',
                            'controllers':controls,'write_value':rhs})
        # A source location after a loop can explain who writes a child even
        # when loop effects preclude a local state edge or timing attribution.
        if on_entry and block.get('issue','').startswith('ST unsupported flow FOR'):
            masked,_=isolate_counted_loops(code,writer_locations=True)
            if masked!=available:
                locations=STParser(masked,condition_parser=source_condition,strict=False,retain_source=True)
                try:locations.seq()
                except ERRORS:pass
                known={s[3] for s in parser.statements}
                for lhs,rhs,condition,offset,_ in locations.statements:
                    if offset not in known:
                        gate=combine('and',outer,condition)
                        external_writer(lhs,rhs,gate,{'source_line':block['source_line'],'st_line':1+code.count('\n',0,offset),
                            'network':str(block['network']),'node':block['node'],'offset':offset,
                            'guard':gate.text(),'guard_complete':False,'limit':'前序循环的效果未经求值'})
        states = defaultdict(set)
        entries = []
        unit = binding.unit(block['scope'])
        def same_type(lhs, rhs):
            kind = binding.type_of(lhs, unit) if unit else None
            return bool(kind and kind.upper() in INTEGER_BOUNDS and kind == binding.type_of(rhs, unit))
        statements = parser.staged_writes(parser.statements, same_type) if block['flow_complete'] else parser.statements
        for lhs, rhs, condition, offset, note in statements:
            if not fixed_path(lhs) or _root(lhs) in loop_writes:
                continue
            gate = combine('and', outer, condition)
            anchors = {a.args[0].casefold() for a in comparisons(gate) if a.kind == 'eq'}
            if not anchors:
                continue
            src = list(_states(gate, anchors))
            operating = combine('and', outer, parser.operating_condition(offset, lhs))
            site = {'operating_guard': '' if operating == TRUE else operating.text(),
                    'commit': getattr(parser, 'commit_sites', {}).get(offset, {}),
                    'source_line': block['source_line'], 'st_line': code.count('\n', 0, offset) + 1,
                    'kind': 'native_structured_st', 'guard': gate.text(),
                    'guard_complete': not block.get('symbolic_external') and (block['flow_complete'] or (block.get('issue', '').startswith('ST unsupported flow ') and offset < intact_prefix)),
                    'network': str(block['network']), 'node': block['node'], 'offset': offset,
                    'frames': parser.frames.get(offset, ()), 'selector': lhs,
                    'selectors': parser.selector_decisions(offset, lhs),
                    'comment': note[0] if note else ''}
            try:
                number = integer(rhs)
            except ValueError:
                number = None
            if any(start <= offset < end for start, end in regions.get(lhs.casefold(), ())):
                site['case_selector'] = lhs
                site['other_condition'] = bool(_conditions(gate, lhs.casefold()))
            current = []
            for key, step in src:
                if key != lhs.casefold():
                    continue
                for row, bound in records(lhs, block['scope'], site, required_cursors(gate)):
                    destination = state_write_value(rhs, key, step, INTEGER_BOUNDS.get((row.declared or '').upper()))
                    if destination is None:
                        if on_unresolved:
                            on_unresolved(row.name, step, dict(bound, write_value=rhs))
                        continue
                    if destination == step:
                        continue
                    current.append((key, step))
                    states[key].add(step)
                    if on_transition is not None:
                        identity = (row.name.casefold(), block['scope'], str(block['network']), block['node'],
                                    token_at.get(offset), step, destination,
                                    tuple((c['instance'], c['source_line'], c.get('node'), c.get('offset')) for c in bound.get('binding', ())))
                        native = native_st.pop(identity, None)
                        if native and native[-1].get('comment') and not bound.get('comment'):
                            bound = dict(bound, comment=native[-1]['comment'])
                        on_transition(row.name, step, destination, bound)
                    row.edge(step, destination, bound)
            if number is not None and (unit := binding.unit(block['scope'])) and unit[0] == 'program':
                target = scan.sysmac_channel(lhs, unit)[0]
                if lhs.casefold() not in anchors:external_writer(lhs,rhs,gate,site)
                for other, value in src:
                    if other == lhs.casefold() or _root(other) in loop_writes:
                        continue
                    reference = scan.sysmac_channel(other, unit)[0]
                    location = {**site, 'scope': block['scope']}
                    result.possible_links.append((reference, value, target, number, 'starts_child', location))
                    for _, step in current:
                        result.possible_links.append((target, step, reference, value, 'waits_for_child', location))
            if not current and rhs.strip().upper() in ('TRUE', 'FALSE'):
                entries.append((src, lhs, rhs, site))
        for src, lhs, rhs, site in entries:
            for var, step in src:
                if step in states.get(var, ()):
                    # Preserve the source spelling, especially a program-local reference.
                    key = next((a.args[0] for a in comparisons(source_condition(site['guard']))
                                if a.args[0].casefold() == var), var)
                    for row, bound in records(key, block['scope'], site):
                        row.effect(step, {**bound, 'target': lhs, 'value': rhs})

    for args in native_st.values():
        on_transition(*args)

    if on_entry:
        watched={r['variable'] for r in model['transitions'] if fixed_path(r['variable'])}
        for site in model.get('range_writes',()):
            for var in sorted(watched):
                sub=re.fullmatch(re.escape(site['base'])+r'\[(-?\d+)\]',var,re.I)
                if sub and site['first']<=int(sub[1])<site['first']+site['count']:
                    for row,bound in records(var,site['scope'],site):
                        if row.declared and row.declared.upper() in INTEGER_BOUNDS:
                            on_entry(row.name,site['value'],bound)

    for item in model.get('native_commands', []):
        if not item['guard_complete'] or not item.get('start'):
            continue
        anchor = re.fullmatch(rf'({IDENT})=(-?\d+)', item['start'])
        if not anchor:
            continue
        var, step = symbol(anchor[1]), int(anchor[2])
        if (var.casefold(), step) not in _states(source_condition(item['guard']), {var.casefold()}):
            continue
        for row, site in records(var, item['scope'], {'source_line': item['source_line'],
                                  'guard': item['guard'], 'kind': item['command'], 'comment': item['comment']}):
            row.effect(step, {**site, 'target': item['arguments']})
    result.issues = {'unsupported': dict(limits), 'binding': dict(binding.limits),
                     'unbound_references': [{'scope': scope, 'reference': name, 'reason': reason}
                                            for scope, name, reason in sorted(binding.unbound)],
                     'adapter_counts': model.get('native_parse_audit', {}).get('counts', model['summary'])}
    if not grid:
        for row in result.candidates.values():
            row.declarations = scan.channel_declarations(row.name)
    binding.paths.cache_clear()
    binding.members_at.cache_clear()
    return result
