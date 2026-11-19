"""Resolve Sysmac state machines in program scope and through typed writable FB calls."""
import re
from collections import Counter, defaultdict
from functools import lru_cache

from plc_trace.analysis.boundary_logic import IDENT, combine, comparisons, integer, symbol
from plc_trace.analysis.successor_graph import STParser, _st_tokens_with_spans
from plc_trace.capture.extract_step_variables import _executable_text
from plc_trace.capture.step_discovery import Discovery, fixed_path, isolate_counted_loops
from plc_trace.capture.station_scope import required_cursors
from plc_trace.source.omron_native import collect_native
from plc_trace.source.plcopen_trace_control import ERRORS, _conditions, _states
from plc_trace.source.smc2_controlflow import collect_smc2
from plc_trace.source.source_expressions import source_condition


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
    def __init__(self, scan, slices):
        self.scan = scan
        self.incoming = defaultdict(list)
        self.limits = Counter()
        for block in slices:
            caller = self.unit(block['scope'])
            if not caller:
                continue
            code = ' '.join(_executable_text(block['code']))
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
                mapping, seen = {}, set()
                for arg in _args(code, match.end()):
                    token = _PIN.fullmatch(arg)
                    if not token:
                        continue
                    formal, operator, actual = token[1].casefold(), token[2], symbol(token[3])
                    if formal in seen:
                        mapping.pop(formal, None)
                        continue
                    seen.add(formal)
                    if (operator == ':=' and scan.directions.get((callee, formal)) == {'inoutvars'}
                            and fixed_path(actual) and self.type_of(actual, caller)
                            and self.type_of(actual, caller) == self.type_of(formal, callee)):
                        mapping[formal] = actual
                self.incoming[callee].append((caller, mapping, {'scope': block['scope'], 'instance': instance,
                    'type': callee[1], 'source_line': block['source_line'], 'reference_actuals': mapping}))

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
            kind, _ = self.scan._peel_type(self.scan.members.get((kind.casefold(), _root(part)), ()), part)
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
                if container and not self.scan.members.get((container.casefold(), _root(field))):
                    issue = f'缺少 {container}.{field} 的叶声明'
            return [(name, kind, issue, [])]
        root = _root(var)
        if unit[0] != 'functionblock' or self.scan.directions.get((unit, root)) != {'inoutvars'}:
            self.limits['template_local_or_nonreference_parameter'] += 1
            return []
        result = []
        for caller, mapping, call in self.incoming[unit]:
            if actual := mapping.get(root):
                for name, kind, issue, chain in self.paths(actual + var[len(root):], caller, active + (unit,)):
                    result.append((name, kind, issue, [*chain, call]))
        if not result:
            self.limits['no_bound_writable_instance'] += 1
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


def discover(source, scan, *, on_transition=None):
    grid = scan.source_format == 'smc2'
    model = collect_smc2(source) if grid else collect_native(source)
    slices = model.get('native_inline_slices', [])
    binding = Bindings(scan, slices)
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
                for token in re.findall(r'__source_predicate_[0-9a-f]+', raw):
                    if token in predicates:
                        raw = raw.replace(token, '(' + predicates[token]['expression'] + ')')
                on_transition(row.name, rel['from'], rel['to'], {
                    **bound, 'guard':raw, 'locator': rel['body'], **({'grid_network': int(network[1])} if network else {})})
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
        regions = _case_regions(code)
        outer = source_condition(block['external_guard'])
        states = defaultdict(set)
        entries = []
        for lhs, rhs, condition, offset, note in parser.statements:
            if not fixed_path(lhs) or _root(lhs) in loop_writes:
                continue
            gate = combine('and', outer, condition)
            anchors = {a.args[0].casefold() for a in comparisons(gate) if a.kind == 'eq'}
            if not anchors:
                continue
            src = list(_states(gate, anchors))
            site = {'source_line': block['source_line'], 'st_line': code.count('\n', 0, offset) + 1,
                    'kind': 'native_structured_st', 'guard': gate.text(), 'guard_complete': block['flow_complete'],
                    'comment': note[0] if note else ''}
            try:
                number = integer(rhs)
            except ValueError:
                number = None
            if any(start <= offset < end for start, end in regions.get(lhs.casefold(), ())):
                site['case_selector'] = lhs
                site['other_condition'] = bool(_conditions(gate, lhs.casefold()))
            current = [(v, a) for v, a in src if v == lhs.casefold() and number is not None and a != number]
            for key, step in current:
                states[key].add(step)
                for row, bound in records(lhs, block['scope'], site, required_cursors(gate)):
                    if on_transition is not None:
                        on_transition(row.name, step, number, bound)
                    row.edge(step, number, bound)
            if number is not None and (unit := binding.unit(block['scope'])) and unit[0] == 'program':
                target = scan.sysmac_channel(lhs, unit)[0]
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
                     'adapter_counts': model.get('native_parse_audit', {}).get('counts', model['summary'])}
    if not grid:
        for row in result.candidates.values():
            row.declarations = scan.channel_declarations(row.name)
    binding.paths.cache_clear()
    return result
