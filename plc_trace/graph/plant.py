"""Source-owned hierarchy and directed inter-program dependencies for the offline atlas."""
from collections import Counter, defaultdict

from plc_trace.capture.step_discovery import fixed_path


def build_plant(units, nets, symbols, sites, calls, tasks):
    # An action's enclosing declared program, FB or function is its owner.
    # Native section names and SMC2 entry names may exist without a child unit.
    named = defaultdict(list)
    for unit in units:
        named[unit['scope'].casefold()].append(unit)
    roots = {scope for scope, items in named.items()
             if any(u['kind'].lower() not in ('action', 'method', 'property') for u in items)}
    groups, routines, routine_ids, group_ids = [], [], {}, {}
    net_routine = []

    def routine(scope):
        key = scope.casefold()
        if key in routine_ids:
            return routine_ids[key]
        ancestor = key
        while ancestor and ancestor not in roots:
            ancestor = ancestor.rpartition('::')[0]
        owner = ancestor or key
        if owner not in group_ids:
            declarations = named.get(owner, [])
            group_ids[owner] = len(groups)
            groups.append({'scope': declarations[0]['scope'] if declarations else scope,
                           'kind': declarations[0]['kind'] if len(declarations) == 1 else 'scope',
                           'routines': []})
        rid = len(routines)
        gid = group_ids[owner]
        group = groups[gid]
        group['routines'].append(rid)
        routines.append({'scope': scope, 'group': gid, 'nets': []})
        routine_ids[key] = rid
        return rid

    for u in units:
        routine(u['scope'])
    for net in nets:
        rid = routine(net['scope'])
        net_routine.append(rid)
        routines[rid]['nets'].append(net['id'])
    net_group = [routines[r]['group'] for r in net_routine]

    # A writable reference passed to a block is an interface binding. It alone
    # cannot establish that the block changed or consumed this value.
    reference_pins = set()
    for net in nets:
        for node in net['nodes']:
            for operand in node.get('operands', ()):
                if operand[0] == 'inout' or node['type'].lower() == 'inoutvariable':
                    pin = operand[2] if len(operand) > 2 else ''
                    reference_pins.add((node['id'], pin, operand[1].casefold()))

    for call in calls:
        for role, actual, pin in call.get('arguments', ()):
            if role == 'inout':
                reference_pins.add((call['node'], f"ST {call['st_line']} · {pin} @{call['st_token']}", actual.casefold()))

    buses = []
    excluded = Counter()
    for sid, symbol in enumerate(symbols):
        if not symbol['writes']:
            continue
        # The members and every array dimension must agree literally. Dynamic
        # subscripts, duplicate holds and undeclared names stay in the read/write index.
        if symbol['scope'] != 'global' or not fixed_path(symbol['name']):
            excluded['scoped_or_nonliteral'] += 1
            continue
        skip = {}
        groups_by_role = {}
        for role in ('writes', 'reads'):
            active = set()
            for i, (net, node, pin) in enumerate(symbol[role]):
                if (node, pin, symbol['name'].casefold()) in reference_pins:
                    skip.setdefault(role, []).append(i)
                else:
                    active.add(net_group[net])
            groups_by_role[role] = sorted(active)
        writers, readers = groups_by_role['writes'], groups_by_role['reads']
        if not writers:
            continue
        gates = [i for i in symbol.get('guard_sites', ()) if sites[i]['net'] is not None
                 and not sites[i].get('binding')]
        gate_groups = sorted({net_group[sites[i]['net']] for i in gates})
        consumers = set(readers) | set(gate_groups)
        if not consumers or len(set(writers) | consumers) <= 1:
            continue
        buses.append({'symbol': sid, 'writers': writers, 'readers': readers,
                      'guard_groups': gate_groups, 'guards': gates, **({'binding_only': skip} if skip else {})})

    call_links = []
    for i, call in enumerate(calls):
        if call['resolved']:
            dest = routine_ids.get(call['targets'][0].casefold())
            if dest is not None:
                call_links.append([i, dest])

    programs = defaultdict(list)
    for rid, entry in enumerate(routines):
        scope = entry['scope'].casefold()
        if len(named[scope]) == 1 and named[scope][0]['kind'].lower() == 'program':
            programs[scope.rsplit('::', 1)[-1]].append(rid)
    task_links = []
    for i, task in enumerate(tasks):
        for inst in task.get('pou_instances', ()):
            name = (inst.get('type_name') or inst.get('name') or '').casefold()
            if len(programs[name]) == 1:
                task_links.append([i, programs[name][0], inst.get('name', '')])

    program_ids = {i for i, group in enumerate(groups) if group['kind'].lower() == 'program'}
    def between_programs(writers, readers):
        w, r = program_ids.intersection(writers), program_ids.intersection(readers)
        return bool(w and r and len(w | r) > 1)

    return {'groups': groups, 'routines': routines, 'net_routine': net_routine,
            'signal_buses': buses, 'call_links': call_links, 'task_links': task_links,
            'coverage': {'cross_signals': len(buses),
                         'cross_guard_signals': sum(any(a != b for a in x['writers'] for b in x['guard_groups'])
                                                    for x in buses),
                         'program_peer_signals': sum(between_programs(b['writers'], b['readers'] + b['guard_groups'])
                                                     for b in buses),
                         'program_peer_guard_signals': sum(between_programs(b['writers'], b['guard_groups']) for b in buses),
                         'resolved_calls': len(call_links), 'task_mounts': len(task_links),
                         **excluded}}
