"""Join sampled state changes to static evidence by full IEC address and literal value."""
import json
from bisect import bisect_right
from collections import Counter, defaultdict, deque
from functools import lru_cache

from plc_trace.capture.extract_step_variables import _SourceScan
from plc_trace.graph.bindings import call_bindings, instance_dependencies
from plc_trace.graph.dependencies import signal_dependencies
from plc_trace.graph.plant import build_plant
from plc_trace.graph.samples import Snapshots
from plc_trace.graph.topology import Topology
from plc_trace.source.plcopen_trace_control import _conditions
from plc_trace.source.source_expressions import source_condition
from plc_trace.source.source_format import detect_source_format
from plc_trace.trace.trace_jump_analyzer import parse_trace
from plc_trace.trace.trace_to_pairs import is_omron_trace_input, parse_omron_trace


def sampled(trace, interval=None, snapshots=None):
    audit, channel_issues = {}, []
    if is_omron_trace_input(trace):
        channels, audit = parse_omron_trace(trace, sample_interval_ms=interval,
                                            observe_recording=snapshots.sysmac if snapshots else None)
    else:
        channels = parse_trace(trace, audit=audit, sample_interval_ms=interval,
                               observe_recording=snapshots.native if snapshots else None, channel_issues=channel_issues)
        audit['skipped_channels'] = channel_issues
    if not any(c['sample_count'] for c in channels.values()):
        raise ValueError('Trace 无实测样本，请选择采集记录')
    recs = [x for x in audit.get('recording_details', []) if 'index_offset' in x]
    offsets = [r['first_index'] + r['index_offset'] for r in recs]
    observations = {}
    for name, data in channels.items():
        key = name.casefold()
        if key in observations:
            raise ValueError('Trace 出现不区分大小写的重复地址：' + name)
        edges = defaultdict(list)
        for index, a, b, stamp in data['jumps']:
            recording = bisect_right(offsets, index) - 1
            rec = recs[recording] if recording >= 0 else {}
            rid, local_index = rec.get('recording_id') or '1', index - rec.get('index_offset',0)
            event = [rid,local_index,round(stamp - rec.get('time_offset_ms',0), 6)]
            if snapshots and (values := snapshots.values.get((key,rid,local_index))):
                event.append(values)
            edges[(a,b)].append(event)
        observations[key] = (name, sorted(data['unique_values']), data['sample_count'], edges)
    return observations, audit


@lru_cache(maxsize=4096)
def condition(raw):
    expr = source_condition(raw)
    def tree(e):
        if e.kind in ('and','or','xor','not'):
            return [e.kind, *[tree(x) for x in e.args]]
        return ['opaque' if e.kind == 'opaque_cmp' or '@' in e.text() else 'term', e.text()]
    return tree(expr), sorted(_conditions(expr, ''), key=lambda n: (n.casefold(), n))


class Locations:
    def __init__(self, nets):
        self.lines, self.cells = defaultdict(list), defaultdict(list)
        self.spans = []
        for net in nets:
            loc = net['id']
            self.lines[net['line']].append((loc,None))
            end = net['line']
            for n in net['nodes']:
                self.lines[n['line']].append((loc,n))
                end = max(end, n['line'])
            self.spans.append((net['line'], end, loc))
            self.cells[(net['scope'],net['label'])].append(loc)
        self.nets = nets
        self.spans.sort()
        self.starts = [s[0] for s in self.spans]

    def find(self, site):
        if 'grid_network' in site:
            opts = self.cells.get((site['scope'],str(site['grid_network'])), [])
            return (opts[0], None) if len(opts) == 1 else (None,None)
        line = site.get('source_line', 0)
        opts = self.lines.get(line, [])
        if opts:
            narrowed = [(i,n) for i,n in opts if self.nets[i]['scope'] == site['scope']]
            opts = narrowed or opts  # FB expansion carries instance paths; XML lines locate definitions.
            ident = str(site.get('node','')).split(':ST:')[0]
            keyed = [(i,n) for i,n in opts if n and n.get('key') == ident]
            opts = keyed or opts
            if len({i for i,_ in opts}) == 1:
                nodes = [n['id'] for _,n in opts if n]
                return opts[0][0], nodes[0] if len(nodes) == 1 else None
            return None, None
        # Inline ST containers may locate a child of the graphical element.
        ix = bisect_right(self.starts,line) - 1
        if ix >= 0:
            start,end,i = self.spans[ix]
            if start < line <= end and self.nets[i]['scope'] == site['scope']:
                return i, None
        return None, None


def portable_literal(value):
    # JSON Numbers are decoded as binary64 by browsers. Keep a large IEC integer exact.
    return str(value) if isinstance(value, int) and abs(value) > (1 << 53) - 1 else value


def scope_key(site):
    return (site['scope'], tuple((c.get('scope'),c.get('instance'),c.get('source_line'))
                                 for c in site.get('binding', ())))


def paths(start, end, scopes):
    """One shortest static example per writer scope. Never stitch unrelated routines."""
    examples = []
    for graph in scopes.values():
        if start not in graph:
            continue
        previous = {start: None}
        queue = deque([start])
        while queue and end not in previous:
            at = queue.popleft()
            for nxt in sorted(graph.get(at, ())):
                if nxt not in previous:
                    previous[nxt] = (at, graph[at][nxt])
                    queue.append(nxt)
        if end in previous and start != end:
            path = []
            at = end
            while at != start:
                at, site_ids = previous[at]
                path.append(site_ids)
            if len(path) >= 2:
                examples.append(list(reversed(path)))
    return examples


def build(source, trace, interval=None, progress=lambda message: None):
    dialect = detect_source_format(source)
    progress('索引全工程例程、网络、端口和操作数')
    top = Topology()
    top.read(source, dialect)
    top.index_operands()
    locations = Locations(top.nets)
    progress('解析写步条件、分支和实例实参')
    scan = _SourceScan(None, dialect, False)
    scan.read(source)
    if dialect == 'plcopen':
        from plc_trace.capture.plcopen_discovery import discover
    else:
        from plc_trace.capture.sysmac_discovery import discover
    impaired = {i['net'] for i in top.issues if i.get('reason') in ('duplicate_source_id','missing_source_id','unknown_output_pin')}
    sites, seen, by_channel = [], set(), defaultdict(lambda: defaultdict(list))
    names = {}
    def witness(name, a, b, site):
        if a == b:
            return
        key = name.casefold()
        # Two instructions with the same guards remain two different write sites.
        ident = (key,a,b,scope_key(site),site.get('source_line'),site.get('st_line'),
                 site.get('node'),site.get('locator'),site['guard'],site['kind'])
        if ident in seen:
            return
        loc, node = locations.find(site)
        gate, reads = condition(site['guard'])
        item = dict(site, net=loc, source_node=node, guard_tree=gate, reads=reads,
                    channel=name, start=a, end=b)
        def fully_parsed(t):
            return t[0] == 'term' or (t[0] != 'opaque' and all(fully_parsed(child) for child in t[1:]))
        item['guard_complete'] = bool(fully_parsed(gate) and loc not in impaired and
                                      site['kind'] != 'plcopen_partial_st_body' and site.get('guard_complete') and not any(
            x in site['guard'] for x in ('@wired_output(', '@condition(', '__source_predicate_')))
        seen.add(ident)
        names[key] = name
        sites.append(item)
    discovery = discover(source, scan, on_transition=witness)
    # Canonical IDs do not depend on hash-randomized traversal of candidates.
    sites.sort(key=lambda s:(s['channel'].casefold(),s['start'],s['end'],s['source_line'],s.get('st_line',0),
                             json.dumps(s,ensure_ascii=False,sort_keys=True)))
    probe = defaultdict(lambda: defaultdict(set))
    explicit_reads = defaultdict(list)
    symbol_keys = sorted(top.symbols)
    symbol_ids = {key:i for i,key in enumerate(symbol_keys)}
    for i,s in enumerate(sites):
        key, pair = s['channel'].casefold(), (s['start'],s['end'])
        by_channel[key][pair].append(i)
        scope = top.nets[s['net']]['scope'] if s['net'] is not None else s['scope']
        references = []
        for operand in s['reads']:
            namespace = top.namespace(scope,operand)
            sym = (namespace,operand.casefold())
            if sym in symbol_ids and not namespace.startswith('unresolved:'):
                references.append([operand,symbol_ids[sym]])
                top.symbols[sym].setdefault('guard_sites',[]).append(i)
            addr = top.sample_address(scope,operand)
            if addr and addr != key:
                probe[key][pair].add(addr)
                if addr in names:
                    explicit_reads[(addr,key)].append(i)
        if references:
            s['signal_refs'] = references
    snapshots = Snapshots(probe)
    progress('读取实测记录，按完整变量地址、采集和采样位置对照')
    observed, audit = sampled(trace, interval, snapshots)
    audit['operand_sample_limits'] = dict(snapshots.limits)
    rows, counts = [], Counter()
    ambiguous = defaultdict(list)
    if dialect == 'smc2':
        for address in names.values():
            _, separator, operand = address.rpartition('::')
            if separator:
                ambiguous[operand.casefold()].append(address)
    for key in sorted(set(observed) | set(by_channel)):
        name, values, samples, hits = observed.get(key, (names.get(key), [], 0, {}))
        pairs = by_channel[key]
        scopes = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for (a,b), indices in pairs.items():
            for idx in indices:
                scopes[scope_key(sites[idx])][a][b].append(idx)
        relations = []
        for a,b in sorted(set(hits) | set(pairs)):
            ids = pairs.get((a,b), [])
            events = hits.get((a,b), [])
            entry = {'from': a, 'to': b, 'sites': ids}
            if events:
                entry['events'] = events
                examples = paths(a,b,scopes) if not ids else []
                if examples:
                    entry['paths'] = examples
                relation = 'direct' if ids else ('path' if examples else 'unresolved')
                counts['observed_pairs'] += 1
                counts['jumps'] += len(events)
            else:
                relation = 'source_only'
            entry['relation'] = relation
            counts[relation] += 1
            relations.append(entry)
        row = {'name': name, 'samples': samples, 'observed_values': values, 'pairs': relations}
        if samples and ambiguous.get(key):
            row['unbound_source_addresses'] = sorted(ambiguous[key])
        rows.append(row)
    summary = dict(counts, nets=len(top.nets), nodes=top.next_node, wires=sum(len(n['wires']) for n in top.nets),
                   source_sites=len(sites), source_units=len(top.units), sampled_operand_events=len(snapshots.values), recorded_channels=len(observed),
                   recordings=audit.get('recordings',len(audit.get('recording_details',[]))),
                   topology_issues=len(top.issues), unlocated_sites=sum(s['net'] is None for s in sites))
    summary['direct_with_multiple_writers'] = sum(e['relation']=='direct' and len(e['sites'])>1
                                                 for c in rows for e in c['pairs'])
    summary['direct_with_only_partial_guards'] = sum(e['relation']=='direct' and
        all(not sites[i]['guard_complete'] for i in e['sites']) for c in rows for e in c['pairs'])
    summary['networks_by_language'] = dict(Counter(n['language'] for n in top.nets))
    summary['partial_guard_sites'] = sum(not s['guard_complete'] for s in sites)
    summary['st_bodies_with_limits'] = sum(bool(n.get('limits') or n.get('other_statements'))
                                            for net in top.nets for n in net['nodes'])
    for site in sites:
        site['start'], site['end'] = portable_literal(site['start']), portable_literal(site['end'])
    for row in rows:
        row['observed_values'] = [portable_literal(v) for v in row['observed_values']]
        for edge in row['pairs']:
            edge['from'], edge['to'] = portable_literal(edge['from']), portable_literal(edge['to'])
            for event in edge.get('events', ()):
                if len(event) == 4:
                    event[3] = {name:portable_literal(v) for name,v in event[3].items()}
    symbols = [dict(top.symbols[key], scope=key[0]) for key in symbol_keys]
    plant = build_plant(top.units, top.nets, symbols, sites, top.calls, top.tasks)
    plant['port_bindings'] = call_bindings(top.nets, top.calls, symbol_ids, top.namespace, top.formals)
    plant['coverage']['calls_with_bound_ports'] = len(plant['port_bindings'])
    roles, boundaries = [], []
    plant['dependencies'] = signal_dependencies(top.nets, symbol_ids, top.namespace, top.program_scopes,
                                                sites=sites, calls=top.calls, roles=roles, boundaries=boundaries)
    plant['dependency_roles'] = roles
    plant['dependency_boundaries'] = boundaries
    bound = instance_dependencies(top, symbol_ids, symbol_keys, plant['port_bindings'], roles)
    plant['dependencies'].extend(bound)
    guards = {}
    plant['st_guards'] = []
    for net in top.nets:
        for node in net['nodes']:
            for item in node.get('assignments', ()):
                raw = item['guard']
                if raw not in guards:
                    guards[raw] = len(guards)
                    plant['st_guards'].append(condition(raw)[0])
                item['guard_id'] = guards[raw]
    plant['coverage']['instance_witnesses'] = len(bound)
    plant['coverage']['dependency_witnesses'] = len(plant['dependencies'])
    plant['coverage']['writes_with_interface_boundaries'] = len(boundaries)
    summary['plant'] = plant['coverage']
    return {'plant': plant, 'schema':'plc.logic_graph.v1','source_name':source.name,'trace_name':trace.name,
            'dialect':dialect,'summary':summary,'trace_audit':audit,'limits':discovery.issues,
            'tasks':top.tasks,'units':top.units,'calls':top.calls,'nets':top.nets,'wiring_issues':top.issues,
            'symbols':symbols,
            'channel_dependencies':[{'read':names[a],'written':names[b],'sites':ids}
                                    for (a,b),ids in sorted(explicit_reads.items())],
            'sites':sites,'channels':rows}
