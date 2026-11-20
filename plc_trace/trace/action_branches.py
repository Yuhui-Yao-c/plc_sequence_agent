"""Project concrete source writes onto actions identified by a recording."""
from collections import defaultdict, deque
from dataclasses import dataclass, field

from plc_trace.analysis.atomic_boundaries import stable_id
from plc_trace.analysis.boundary_logic import combine, compatible
from plc_trace.source.source_expressions import source_condition
from plc_trace.trace.source_paths import example_route, returns_to_earlier_rung, shape, source_paths


@dataclass
class Write:
    channel: str
    start: int
    end: int
    site: dict
    id: str = field(init=False)
    body: tuple = field(init=False)
    fragment: tuple = field(init=False)
    dispatch: tuple = field(init=False)
    frames: tuple = field(init=False)
    ports: frozenset = field(init=False)

    def __post_init__(self):
        s = self.site
        self.channel = self.channel.casefold()
        calls = tuple((c['scope'], c['instance'], c['source_line'], c.get('node', ''), c.get('offset', -1)) for c in s.get('binding', ()))
        self.body = (self.channel, s.get('program', ''), s['scope'], calls, s.get('body', ''))
        self.fragment = (*self.body, s.get('network', ''), s.get('node', '').split(':ST:')[0], s['source_line'])
        self.frames = tuple(tuple(f) for f in s.get('frames', ()))
        selector = s.get('selector', self.channel).casefold()
        self.dispatch = ()
        for i, (kind, decision, _, var) in enumerate(self.frames):
            if kind == 'case' and var.casefold() == selector:
                self.dispatch = (*self.fragment, self.frames[:i], decision)
        self.ports = frozenset((*self.body, s.get('network', ''), node, port)
                               for var, value, node, port in s.get('selector_ports', ())
                               if var.casefold() == selector and value == self.start)
        location = [self.fragment, s.get('offset', -1), s.get('locator', '')]
        self.id = stable_id('BR', [location, self.start, self.end])

    @property
    def complete(self):
        return (self.site.get('guard_complete', False) and not self.site.get('write_conflict')
                and not any(p in self.site['guard'] for p in ('@wired_output(', '@condition(', '__source_predicate_')))

    def decision(self, other):
        if self.channel != other.channel or self.start != other.start or self.body != other.body:
            return ''
        if self.fragment == other.fragment:
            mine, theirs = self.site.get('selectors', ()), other.site.get('selectors', ())
            if mine != theirs or self.dispatch != other.dispatch:
                return ''
            boundary = mine[-1] if mine else -1
            for left, right in zip(self.frames, other.frames):
                if left == right:
                    continue
                if left[:2] == right[:2] and left[2] != right[2] and left[1] >= boundary:
                    return f'{left[0].upper()}@{left[1]}'
                break
        common = self.ports & other.ports
        if common:
            # A forked wire can enable two independent MOVEs. Require exclusive
            # conditions before treating its destinations as alternatives.
            gate = combine('and', source_condition(self.site['guard']), source_condition(other.site['guard']))
            if not compatible(gate, self.site.get('selector', self.channel), self.start, check_remaining=True):
                return 'EQ ' + '.'.join(min(common)[-2:])
        return ''


class BranchIndex:
    def __init__(self, writes, unresolved=None, entries=None):
        self.unresolved = unresolved or {}
        self.entries = entries or {}
        self.writes = {}
        self.outgoing = defaultdict(list)
        self.incoming = defaultdict(list)
        self.calls_by_port = defaultdict(list)
        for edge in writes:
            if edge.id in self.writes:
                if self.writes[edge.id].site != edge.site:
                    raise ValueError(f'写点身份冲突 {edge.id}')
                continue
            self.writes[edge.id] = edge
            self.outgoing[edge.channel, edge.start].append(edge)
            self.incoming[edge.channel, edge.end].append(edge)
            binding = edge.site.get('binding', ())
            if binding:
                call = binding[-1]
                ports = call.get('selector_ports', ())
                # The caller must select the actual child's specific entry step. A
                # shared FB type, handshake signal, or ungated call cannot open it.
                selector = edge.site.get('selector', '').casefold()
                if any(v.casefold() == selector and n == edge.start for v, n, _, _ in ports):
                    hosts = tuple((c['scope'], c['instance'], c['source_line'], c.get('node', ''), c.get('offset', -1)) for c in binding[:-1])
                    for var, n, node, port in ports:
                        key = (edge.site.get('program', ''), call['scope'], hosts, call['body'],
                               call['network'], node, port, var.casefold(), n)
                        self.calls_by_port[key].append(edge)
        for edges in self.outgoing.values():
            for i, first in enumerate(edges):
                for second in edges[i+1:]:
                    same_st = (first.site.get('offset', -1) >= 0 and second.site.get('offset', -1) >= 0
                               and first.fragment == second.fragment)
                    if not (same_st or first.ports & second.ports) or first.decision(second):
                        continue
                    condition = combine('and', source_condition(first.site['guard']), source_condition(second.site['guard']))
                    if compatible(condition, first.site.get('selector', first.channel), first.start, check_remaining=True):
                        first.site['write_conflict'] = second.id
                        second.site['write_conflict'] = first.id

    @staticmethod
    def same_route(first, edge):
        # An ST dispatch has a definite lexical boundary. Different CASEs or
        # call instances do not form a path merely by sharing a numeric state.
        if first.frames and edge.frames and (first.dispatch or edge.dispatch):
            return first.dispatch == edge.dispatch
        return first.body == edge.body

    def routes(self, channel, start, end, budget=4096):
        """Enumerate competing simple paths (including direct writes), with an explicit unknown on overflow."""
        reachable = {end}
        todo = [end]
        while todo:
            for edge in self.incoming[channel, todo.pop()]:
                if edge.start not in reachable:
                    reachable.add(edge.start)
                    todo.append(edge.start)
        stack = [(edge, (), {start}) for edge in self.outgoing[channel, start] if edge.end in reachable]
        routes, retries = [], []
        while stack:
            if budget == 0 or len(routes) >= 32:
                return routes, True, retries
            edge, path, visited = stack.pop()
            budget -= 1
            path = (*path, edge)
            if edge.end == end:
                routes.append(path)
            elif edge.end in visited:
                if path[0].dispatch and all(e.dispatch == path[0].dispatch for e in path):
                    retries.append(path)
            else:
                visited = visited | {edge.end}
                stack.extend((child, path, visited) for child in self.outgoing[channel, edge.end]
                             if child.end in reachable and self.same_route(path[0], child))
        return routes, False, retries

    def project(self, observations):
        """Return admissions with their trace witnesses and one disposition per observed pair."""
        admitted, dispositions = defaultdict(dict), {}
        observed_states = defaultdict(set)
        for channel, start, end in observations:
            observed_states[channel].update((start, end))
        for anchor in observations:
            channel, start, end = anchor
            paths, limited, retries = self.routes(channel, start, end)
            cross = example_route(self, channel, start, end, handoff=True)
            entries = self.entries.get((channel, end), ())
            origin = source_paths(self, anchor, paths, limited, cross, entries)
            same_steps = (bool(paths) and len(paths[0])>1 and
                          all(shape(path)==shape(paths[0]) for path in paths))
            returning = any(returns_to_earlier_rung(path) for path in paths)
            # A retry before the target can be invisible between samples. Require
            # a common dispatch and a genuine branch on a route to the endpoint.
            cycles = [r for r in retries if any(r[0].dispatch == p[0].dispatch and any(
                        x.start == y.start and x.decision(y) for x in r for y in p) for p in paths)]
            direct = [p[0] for p in paths if len(p) == 1]
            uncertain = [site for value in {start, *(e.start for p in paths for e in p)}
                         for site in self.unresolved.get((channel, value), ())]
            if uncertain:
                state = '动态写步目标待核'
            elif limited:
                state = '路径搜索未收敛'
            elif entries and (paths or cross):
                state = '写入来源待辨'
            elif cross:
                state = '端点已见，分支待辨' if paths else '跨动作段接续'
            elif entries:
                state = '其他逻辑写入终值'
            elif not paths:
                state = '缺少对应写点或路径'
            elif cycles:
                state = '端点间可能重试'
            elif returning and all(returns_to_earlier_rung(path) for path in paths):
                state = '返回前序梯级接续'
            elif origin['intermediate_writers']:
                state = '路径含其他写入来源'
            elif same_steps:
                state = '端点间存在中间步'
            elif len(paths) > 1:
                state = '端点已见，分支待辨'
            elif any(not edge.complete for edge in paths[0]):
                state = '写入或门控待核'
            else:
                state = '分支已观测' if len(paths[0]) == 1 else '端点间存在中间步'
            candidates = {edge.id for path in (*paths, *cycles) for edge in path}
            candidates.update(ident for route in origin['routes'] for hop in route['hops'] for ident in hop)
            dispositions[anchor] = {'state': state, 'candidates': sorted(candidates), 'unresolved': uncertain, 'origin': origin}
            seeds = []
            if state == '分支已观测':
                seeds = list(paths[0])
            elif state == '写入或门控待核' and len(paths)==1 and len(direct)==1 and direct[0].ports:
                # The selector and sink can be definite even when a feedback
                # signal required inside the rung is absent from the recording.
                seeds=direct
            elif state == '端点间存在中间步':
                # One value chain may contain multiple physical writes to the
                # same destination. Admit alternatives only at a shared decision.
                for hop in zip(*paths):
                    sites=list({e.id:e for e in hop}.values())
                    if (len(sites)==1 or set.intersection(*(set(e.ports) for e in sites)) or
                            all(a.decision(b) for i,a in enumerate(sites) for b in sites[i+1:])):
                        seeds.extend(sites)
            elif state in ('端点已见，分支待辨', '端点间可能重试') and not cross and not returning:
                # All explanations must enter the same concrete decision. Different
                # owners remain exclusively in the observation review.
                first = {p[0].id: p[0] for p in paths}
                if all(e.complete for e in first.values()) and all(
                        a.decision(b) for i, a in enumerate(first.values()) for b in list(first.values())[i+1:]):
                    seeds = list(first.values())
            for seed in seeds:
                admitted[seed.id][anchor] = {'reason': state, 'seed': seed.id}
            if state == '分支已观测':
                dispositions[anchor]['branch'] = direct[0].id
            incomplete = {other.id for seed in seeds for other in self.outgoing[channel, seed.start]
                          if other.id != seed.id and not other.complete and
                          (seed.decision(other) or (other.end != seed.end and seed.ports & other.ports))}
            dispositions[anchor]['pending_exits'] = sorted(incomplete)
            for seed in seeds:
                self._alternatives(seed, anchor, admitted, observed_states[channel])
                if not seed.complete:
                    continue
                for port in seed.ports:
                    key = (*port[1:], seed.site.get('selector', channel).casefold(), seed.start)
                    for child in self.calls_by_port.get(key, ()):
                        if child.channel != channel and child.complete:
                            call = child.site['binding'][-1]
                            admitted[child.id].setdefault(anchor, {'reason': '同一动作显式调用的子步',
                                'seed': seed.id, 'decision': f"{call['instance']}@{call['source_line']}"})
        return dict(admitted), dispositions

    def _alternatives(self, seed, anchor, admitted, observed):
        if seed.site.get('write_conflict'):
            return
        siblings = []
        for other in self.outgoing[seed.channel, seed.start]:
            witness = seed.decision(other)
            if witness and not other.site.get('write_conflict') and (other.complete or other.ports or other.dispatch):
                siblings.append(other)
                admitted[other.id].setdefault(anchor, {'reason': '同一判断的其他出口', 'seed': seed.id, 'decision': witness})
        for other in siblings:
            if not other.dispatch or not other.complete:
                continue
            # Find nodes reachable inside this arm, then remove any node having
            # an external entry. Removing a join also cuts off its successors.
            # Internal back edges survive this test and can represent retries.
            region = {}
            todo = [other.end]
            stop = observed | {s.end for s in siblings if s.id != other.id}
            while todo:
                step = todo.pop()
                if step in stop or step in region:
                    continue
                exits = self.outgoing[seed.channel, step]
                if all(e.dispatch == other.dispatch and e.complete for e in exits):
                    region[step] = exits
                    todo.extend(e.end for e in exits)
            outside = deque(s for s in region if any(p.id != other.id and
                            (p.start not in region or p.dispatch != other.dispatch)
                            for p in self.incoming[seed.channel, s]))
            while outside:
                step = outside.popleft()
                outside.extend(e.end for e in region.pop(step, ()) if e.end in region)
            queue = deque([other])
            while queue:
                parent = queue.popleft()
                for child in region.pop(parent.end, ()):
                    admitted[child.id].setdefault(anchor, {'reason': '同一出口的内部步', 'seed': seed.id,
                                                           'parent': parent.id, 'decision': seed.decision(other)})
                    queue.append(child)
