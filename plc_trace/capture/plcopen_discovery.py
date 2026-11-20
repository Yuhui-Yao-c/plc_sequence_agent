"""Nominate selectors and writable FB actuals, then index wired/ST evidence in their caller."""
import re
from collections import defaultdict
from functools import lru_cache

from lxml import etree

from plc_trace.analysis.boundary_logic import IDENT, integer, symbol
from plc_trace.analysis.fb_transport import bindings, interface
from plc_trace.analysis.successor_graph import STParser, WireBody, pins, tag, text
from plc_trace.capture.extract_step_variables import _executable_text
from plc_trace.capture.step_discovery import Discovery, fixed_path, isolate_counted_loops
from plc_trace.capture.station_scope import at_path, declaration, required_cursors
from plc_trace.source.source_expressions import source_condition
from plc_trace.source.plcopen_trace_control import ERRORS, _bodies, _scope, collect_root

_NUMBER = r'(?:[a-z]+#)?[-+]?\d+\b'
_SELECTORS = [re.compile(p, re.I) for p in (
    rf'\bCASE\s+({IDENT})\s+OF\b', rf'(?<![\w.#])({IDENT})\s*={{1,2}}\s*{_NUMBER}',
    rf'(?<![\w.#]){_NUMBER}\s*={{1,2}}\s*({IDENT})')]


def _nominations(root):
    units = list(root.iter('{*}pou', '{*}action', '{*}method'))
    templates = defaultdict(list)
    for unit in units:
        if unit.get('pouType', '').lower() == 'functionblock':
            templates[unit.get('name', '').casefold()].append(unit)
    members, calls = defaultdict(dict), defaultdict(list)
    for unit in units:
        owner = next((p for p in (unit, *unit.iterancestors())
                      if p.get('pouType', '').lower() == 'functionblock'), None)
        key = owner.get('name', '').casefold() if owner is not None else ''
        def add(value):
            name = symbol(value)
            if re.fullmatch(IDENT, name):
                members[key].setdefault(name.casefold(), name)
        for body in _bodies(unit):
            if tag(body) == 'ST':
                codes = [text(body)]
            else:
                wire = WireBody(body)
                codes = [n.text for n in body.iter('{*}STCode', '{*}expression', '{*}variable') if n.text]
                for node in body.iter('{*}block'):
                    name = node.get('typeName', '').casefold()
                    if name in ('eq', '=', '=='):
                        try:
                            args = [wire.value(p) for p in pins(node, 'inputVariables')
                                    if p.get('formalParameter', '').upper() != 'EN']
                            if len(args) == 2:
                                for x, y in (args, args[::-1]):
                                    if integer(y) is not None:
                                        add(x)
                        except ERRORS:
                            continue
                    if len(templates.get(name, ())) == 1 and node.get('instanceName'):
                        actuals, _ = bindings(node, wire)
                        calls[key].append((name, actuals))
            for code in codes:
                clean = ' '.join(_executable_text(code))
                for pattern in _SELECTORS:
                    for match in pattern.finditer(clean):
                        add(match[1])
    interfaces = {key: interface(options[0]) for key, options in templates.items() if len(options) == 1}
    @lru_cache(maxsize=None)
    def expand(key, active):
        result = members[key].copy()
        for callee, args in calls[key]:
            if callee in active:
                continue
            for item in expand(callee, active | {callee}).values():
                formal = re.split(r'[.\[]', item, 1)[0]
                param = interfaces[callee].get(formal.casefold(), {})
                actual = args.get(formal.casefold(), '')
                if param.get('direction') == 'inOutVars' and fixed_path(actual):
                    name = actual + item[len(formal):]
                    result.setdefault(name.casefold(), name)
        return result
    return expand('', frozenset())


def _site(item):
    site = {k: item[k] for k in ('scope', 'source_line', 'st_line', 'guard', 'guard_complete', 'comment')
            if item.get(k) or k == 'guard_complete'}
    if item.get('instance_path'):
        site['binding'] = [{'scope': c['scope'], 'instance': c['instance'], 'type': c['type'], 'source_line': c['source_line'],
                            'reference_actuals': {k: c['actuals'][k] for k in c['reference_formals']},
                            **{k: c[k] for k in ('node', 'body', 'network', 'guard', 'selector_ports')}}
                           for c in item['instance_path']]
    return site


def _channel_declaration(types, variable, program):
    """Validate every static subscript against the declared dimensions."""
    facts = []
    parts = variable.split('.')
    root = re.split(r'\[', parts[0], 1)[0]
    nodes = types.locals.get(program, {}).get(root, types.globals.get(root, ()))
    for pos, part in enumerate(parts):
        if len(nodes) == 1:
            variable_node = nodes[0].getparent().getparent()
            facts.extend(at_path(declaration(variable_node), parts, pos))
        typ = types._unaliased(nodes)
        for indexes in re.findall(r'\[([^]]*)\]', part):
            if typ is None or tag(typ) != 'array':
                return None, facts
            dims = typ.findall('{*}dimension')
            if len(dims) != len(indexes.split(',')):
                return None, facts
            for dim, ix in zip(dims, indexes.split(',')):
                try:
                    if not int(dim.get('lower')) <= int(ix) <= int(dim.get('upper')):
                        return None, facts
                except (TypeError, ValueError):
                    return None, facts
            base = typ.find('{*}baseType')
            typ = types._unaliased(list(base) if base is not None else ())
        if typ is None:
            return None, facts
        if pos == len(parts) - 1:
            return (tag(typ).upper() if types.bounds(variable, program) else None), facts
        if tag(typ) != 'struct':
            return None, facts
        field = parts[pos + 1].split('[')[0]
        nodes = types._declarations(typ).get(field, ())
    return None, facts


def _recover_st(code, parser, error):
    """Keep the parsed prefix. Beyond a simple counted loop, exclude every root it can write."""
    if not str(error).startswith('ST unsupported flow '):
        return []
    prefix = list(parser.statements)
    if str(error) != 'ST unsupported flow FOR':
        return prefix
    isolated, touched = isolate_counted_loops(code)
    if not touched:
        return prefix
    later = STParser(isolated, condition_parser=source_condition, strict=False, retain_source=True)
    try:
        later.seq()
    except ERRORS:
        pass  # Only statements actually parsed before the next unsupported control.
    last = prefix[-1][3] if prefix else -1
    return prefix + [s for s in later.statements if s[3] > last and re.split(r'[.\[]', s[0], 1)[0].casefold() not in touched]


def discover(source, scan, *, on_transition=None, on_unresolved=None):
    root = etree.parse(str(source), etree.XMLParser(huge_tree=True, no_network=True, resolve_entities=False)).getroot()
    names = _nominations(root)
    ci = collect_root(root, names, recover_st=_recover_st)
    pous = {_scope(p): p for p in root.iter('{*}pou')}
    result = Discovery()
    result.visible.update(name for name, _, _ in scan.references.values())
    result.visible.update(scan.dynamic.values())
    # Detached vendor actions may lack a lexical POU owner in this scan. Test
    # every plausible local spelling when deciding whether a regex is exact.
    local_owners = defaultdict(set)
    for path, pou in pous.items():
        if pou.get('pouType', '').casefold() == 'program':
            for key in ci.types.locals.get(path, {}):
                local_owners[key].add(pou.get('name'))
    for name, _, _ in scan.references.values():
        key = re.split(r'[.\[]', name, 1)[0].casefold()
        result.visible.update(f'{owner}.{name}' for owner in local_owners.get(key, ()))

    @lru_cache(maxsize=None)
    def declared_type(key, program):
        return _channel_declaration(ci.types, key, program)

    def resolve(key, item, *, writer=False):
        program = item['program']
        spelling = names[key]
        prefix = re.split(r'[.\[]', key, 1)[0]
        unit = pous.get(program)
        local = prefix in ci.types.locals.get(program, {})
        owner = unit.get('name') if unit is not None else program.rsplit('::', 1)[-1]
        name = f'{owner}.{spelling}' if local else spelling
        candidate = result.candidate(name)
        if local and (unit is None or unit.get('pouType', '').casefold() != 'program'):
            candidate.reasons.add('local_without_program_instance')
        candidate.declared, candidate.declarations = declared_type(key, program)
        if not candidate.declared:
            candidate.type_issue = '缺少唯一且受支持的整数叶类型或数组声明'
        if writer:
            call = item['instance_path'][0] if item['instance_path'] else item
            routine = call['scope'].rsplit('::', 1)[-1]
            candidate.owners.add((owner, routine, call['source_line']))
        return candidate

    if on_unresolved:
        for (key, start), options in ci.state_writes.items():
            for item in options:
                if item['to'] is None:
                    on_unresolved(resolve(key, item).name, start, item)
    for key, item in ci.edges:
        candidate = resolve(key, item, writer=True)
        site = {**_site(item), 'program': item['program'], 'kind': item['grade']}
        others = (names.keys() & {n.casefold() for n in item['numeric_conditions']}) - {key}
        if others:
            required = required_cursors(source_condition(item['guard'])) & others
            site['co_cursors'] = sorted({resolve(peer, item).name.casefold() for peer in required})
        if on_transition is not None:
            on_transition(candidate.name, item['from'], item['to'], {
                **site, 'selector': names[key], 'source_id': item['source_id'], **{k: item[k] for k in ('body', 'network', 'node', 'write_value',
                                                 'condition_symbols', 'output_dependencies', 'offset', 'frames', 'selector_ports', 'operating_guard', 'commit', 'selectors')}})
        candidate.edge(item['from'], item['to'], site)
        for other, conditions in item['numeric_conditions'].items():
            if other.casefold() not in names or other.casefold() == key:
                continue
            peer = resolve(other.casefold(), item).name
            for condition in conditions:
                if condition['operator'] == '=' and re.fullmatch(r'-?\d+', condition['value']):
                    result.possible_links.append((candidate.name, item['from'], peer, int(condition['value']),
                                                  'waits_for_child', site))
    for (key, step), commands in ci.commands.items():
        for item in commands:
            candidate = resolve(key, item)
            candidate.effect(step, {**_site(item), 'kind': item['command'], 'target': item['arguments'],
                                    'value': item['write_value']})
    # Use only witnessed control writes for automatic routing. Initialisation,
    # display copies and uncalled program bodies do not establish scheduling.
    programs = scan.program_writes
    for definitions in (*programs.roots.values(), *programs.actions.values()):
        for routine in definitions:
            routine.writes.clear()
    for key, candidate in result.candidates.items():
        for owner, routine, line in sorted(candidate.owners):
            definitions = (programs.roots.get(owner.casefold(), ()) if routine.casefold() == owner.casefold() else
                           programs.actions.get((owner.casefold(), routine.casefold()), ()))
            if len(definitions) == 1:
                definitions[0].writes.setdefault(key, line)
    if scan.tasks:
        channels = {key: {'name': row.name} for key, row in result.candidates.items()}
        programs.bind(scan.tasks, channels)
        for task in scan.tasks:
            for name, witness in task['channel_writes'].items():
                result.candidates[name.casefold()].scheduled[task['name']] = witness
        for row in result.candidates.values():
            if row.edges and not row.scheduled:
                row.reasons.add('no_reachable_control_writer_in_exported_schedule')
    result.issues = dict(sorted(ci.issues.items()))
    return result
