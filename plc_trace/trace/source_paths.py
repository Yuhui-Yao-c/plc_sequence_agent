"""Describe source routes between samples without assigning time or admitting branches."""
from collections import deque


def returns_to_earlier_rung(path):
    orders = [edge.site.get('rung_order') for edge in path]
    return any(a is not None and b is not None and b < a for a, b in zip(orders, orders[1:]))


def shape(path):
    return tuple((edge.body, edge.dispatch, edge.start, edge.end) for edge in path)


def route_kind(path):
    if len(path) == 1:
        return '直接写步'
    if any(a.body != b.body for a, b in zip(path, path[1:])):
        return '同程序跨动作段'
    if returns_to_earlier_rung(path):
        return '返回前序梯级'
    return '同段连续写步'


def explicit_handoff(previous, current):
    a, b = previous.site.get('invocation'), current.site.get('invocation')
    return (a and b and previous.channel == current.channel and a['program'] == b['program']
            and a['action'] != b['action'] and a['order'] < b['order'])


def example_route(index, channel, start, end, *, handoff):
    """A shortest witness. Reachability here never grants admission to a delivery row."""
    queue = deque()
    seen = set()
    targets = [edge.site['invocation'] for edge in index.incoming[channel, end] if edge.site.get('invocation')]
    for edge in index.outgoing[channel, start]:
        if handoff:
            invocation = edge.site.get('invocation')
            if not invocation or not any(t['program'] == invocation['program'] and t['order'] > invocation['order'] for t in targets):
                continue
        queue.append(((edge,), edge, False))
    while queue:
        path, segment, crossed = queue.popleft()
        last = path[-1]
        if last.end == end:
            if crossed == handoff:
                return path
            continue
        key = (last.end, last.body, last.dispatch, segment.body, segment.dispatch, crossed)
        if key in seen:
            continue
        seen.add(key)
        for child in index.outgoing[channel, last.end]:
            if index.same_route(segment, child) and index.same_route(last, child):
                queue.append(((*path, child), segment, crossed))
            elif handoff and explicit_handoff(last, child):
                queue.append(((*path, child), child, True))
    return ()


def source_paths(index, anchor, paths, limited, cross, entries):
    candidates = list(paths)
    if limited:
        shortest = example_route(index, *anchor, handoff=False)
        if shortest:
            candidates.insert(0, shortest)
    if cross:
        candidates.append(cross)
    groups = {}
    for path in candidates:
        key = shape(path)
        if key not in groups:
            groups[key] = {
                'steps': [path[0].start, *(e.end for e in path)],
                'scopes': list(dict.fromkeys(e.site['scope'] for e in path)),
                'kind': route_kind(path),
                'hops': [set() for _ in path],
            }
        for hop, edge in zip(groups[key]['hops'], path):
            hop.add(edge.id)
    ordered = sorted(groups.values(), key=lambda r: (len(r['steps']), r['steps'], r['scopes']))
    for route in ordered:
        route['hops'] = [sorted(h) for h in route['hops']]
    # Always retain an explicit caller witness even when there are many local routes.
    shown = ordered[:3]
    if cross:
        crossed = groups[shape(cross)]
        if crossed not in shown:
            shown.append(crossed)
    if shown:
        kind = shown[0]['kind']
    elif entries:
        kind = '批量写入' if all(s['kind']=='array_fill' for s in entries) else '上层程序写入'
    else:
        kind = '来源待核'
    interior = sorted({step for route in ordered for step in route['steps'][1:-1]})
    intermediate_writers = [{'value':step,'sites':index.entries[anchor[0],step]}
                            for step in interior if (anchor[0],step) in index.entries]
    return {'kind':kind, 'routes':shown, 'other_routes':len(ordered)-len(shown),
            'local_search_limited':limited, 'entry_writers':entries, 'intermediate_writers':intermediate_writers}
