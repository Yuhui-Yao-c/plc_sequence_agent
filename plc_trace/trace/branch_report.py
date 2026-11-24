"""Create the action-scoped delivery ledger while retaining the measured endpoint ledger."""
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from plc_trace.capture.write_index import write_index
from plc_trace.source.plcopen_trace_control import _conditions, _numeric_conditions, step_comment_text
from plc_trace.source.source_expressions import source_condition
from plc_trace.trace.action_branches import BranchIndex, Write

BRANCH_FILE = 'trace_action_branches.csv'
EVIDENCE_FILE = 'trace_branch_evidence.json'
OBSERVATIONS_FILE = 'trace_branch_observations.csv'
EVENTS_FILE = 'trace_branch_events.csv'
BRANCH_FIELDS = ['分支ID', '本次状态', '关联实测', '收录依据', '源码位置', '适用工况']
TIME_FIELDS = ('发生次数', '有效计时次数', '中位数时长ms', '最小时长ms', '最大时长ms',
               '首次时间ms', '末次时间ms', '所属周期数', '样例跳变索引', '样例采集ID',
               '采集首段未计时次数', '缺口后未计时次数', '估算时长次数')


def dump_rows(path, fields, records):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)


def row_key(row):
    return row.get('分支ID') or f"{row['开始标签']}|||{row['结束标签']}"


def observation_display_rows(observations):
    """Keep each measured pair once. An assigned branch already carries that measurement."""
    fields = ('构成要素', '控制器', '证据POU', '开始标签', '结束标签', '分支判定', '跳变来源',
              '源码步序路径', '所属程序段', '其他写入来源', *TIME_FIELDS)
    for row in observations:
        if not row['分支ID']:
            yield {**{key: row[key] for key in fields}, '状态': 'trace_observation', '本次状态': '实测跳变'}


def delivery_evidence(run):
    path = Path(run) / EVIDENCE_FILE
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8'))['branches']
    from plc_trace.trace.trace_source_evidence import read_trace_evidence
    return read_trace_evidence(run)


def location(site):
    pos = f"XML {site['source_line']}"
    if site.get('st_line'):
        pos += f" / ST {site['st_line']}"
    elif site.get('node'):
        pos += ' / ' + site['node']
    return f"{site['scope']} · {pos}"


def describe(index, edge, status):
    site = edge.site
    state = (edge.channel, edge.start)
    source_id = site.get('source_id', edge.id)
    selector = site.get('selector', edge.channel).casefold()
    guard = source_condition(site['guard'])
    direct = {**site, 'source_id': source_id, 'instance_path': site.get('binding', ()),
              'from': edge.start, 'to': edge.end, 'grade': site['kind'], 'path': '',
              'condition_symbols': sorted(_conditions(guard, selector)), 'numeric_conditions': _numeric_conditions(guard, selector)}
    commands, notes, issues = [], [], []
    description = step_comment_text(site.get('comment', ''), edge.start)
    refs = [source_id] if description else []
    if index.control is not None:
        matches = [e for e in index.control.state_writes.get(state, ()) if e['source_id'] == source_id]
        if len(matches) == 1:
            desc, exact_refs, commands, source_notes, issues = index._control_description(
                state, 'direct', matches, index.commands.get(state, ()))
            notes = [n for n in source_notes if n['source_id'] == source_id]
            commands = [c for c in commands if c.get('instance_path', ()) == matches[0].get('instance_path', ())]
            if desc:
                description, refs = desc, exact_refs
    elif not site.get('frames'):
        # Native commands carry a real rung locator. Reusing commands across ST arms
        # needs instruction-level membership, which this older command ledger lacks.
        commands = [c for c in index.commands.get(state, ()) if c['scope'] == site['scope']
                    and c['network'] == site.get('network') and c.get('guard_complete')
                and not site.get('binding')]
    context = {} if description else index.context.packet(
        state, (edge.channel, edge.end), 'direct', [direct], [], commands, notes, issues,
        branch={'id': edge.id, 'status': status})
    return {'relation': 'direct', 'direct': [direct], 'candidate_path': [], 'start_commands': commands,
            'linked_commands': commands, 'start_notes': notes, 'start_state_writes': [direct],
            'description': description, 'description_source': 'A档(源码控制注释)' if description else '',
            'description_refs': refs, 'source_issues': issues, 'description_context': context,
            'basis': context.get('basis', '')}


def number(s):
    try:
        return int(s)
    except ValueError:
        return float(s)


def origin_columns(origin):
    routes = origin['routes']
    scope = list(dict.fromkeys(s for r in routes for s in r['scopes']))
    entries = origin['entry_writers']
    paths = list(dict.fromkeys(' → '.join(map(str,r['steps'])) for r in routes))
    if origin['other_routes']:paths.append(f"另有 {origin['other_routes']} 条源码路径")
    if origin['local_search_limited']:paths.append('搜索达到上限')
    def writer(site):
        return location(site)+' '+(site.get('guard') or '使能条件待核')
    alternatives = [writer(s) for s in entries]
    for block in origin['intermediate_writers']:
        alternatives.extend(f"中间值 {block['value']} · {writer(s)}" for s in block['sites'])
    return {'跳变来源':origin['kind'], '源码步序路径':' / '.join(paths),
            '所属程序段':'、'.join(dict.fromkeys([*scope,*(s['scope'] for s in entries)])),
            '其他写入来源':'\n'.join(alternatives)}


def prepare(run, source, raw, jumps, source_index, sampled_names, reuse=None):
    from plc_trace.trace.trace_to_pairs import PAIR_FIELDS
    run = Path(run)
    observations = {}
    names = {name.casefold(): name for name in sampled_names}
    sampled = set(names)
    for row in raw:
        channel = row['控制器'].casefold()
        names[channel] = row['控制器']
        a, b = (row[k].rsplit('=', 1)[-1] for k in ('开始标签', '结束标签'))
        observations[channel, number(a), number(b)] = row
    writes = []
    unresolved = defaultdict(list)
    cached = write_index(source, run, reuse)
    for name, start, site in cached['unresolved']:
        if name.casefold() in sampled:
            unresolved[name.casefold(), start].append(site)
    for name, start, end, site in cached['writes']:
        if name.casefold() in sampled or any(c.get('selector_ports') for c in site.get('binding', ())):
            names.setdefault(name.casefold(), name)
            writes.append(Write(name, start, end, site))
    entries = defaultdict(list)
    for name, value, site in cached['entries']:
        if name.casefold() in sampled:
            entries[name.casefold(),value].append(site)
    index = BranchIndex(writes, unresolved, entries)
    admitted, outcomes = index.project(observations)
    links = {}
    event_counts = Counter()
    def event_rows():
        for row in jumps:
            key = (row['变量'].casefold(), row['from值'], row['to值'])
            outcome = outcomes[key]
            event_counts[key] += 1
            yield {**row, '采集ID': row.get('采集ID') or '1', '分支判定': outcome['state'],
                   '分支ID': outcome.get('branch', '')}
    dump_rows(run / EVENTS_FILE, ['变量', 'from值', 'to值', '采集ID', '样本索引', '时间戳ms',
                                  '计时状态', '有效停留ms', '分支判定', '分支ID'], event_rows())
    rows, evidence, states = [], {}, Counter()
    for ident, anchors in sorted(admitted.items()):
        edge = index.writes[ident]
        key = (edge.channel, edge.start, edge.end)
        raw_row = observations.get(key, {})
        own = outcomes.get(key, {})
        measured = own.get('branch') == ident
        if not measured and ident in own.get('candidates', ()):
            anchors.setdefault(key, {'reason': own['state'], 'seed': ident})
        if edge.channel not in sampled:
            status = '本次未采该通道'
        elif measured:
            status = '分支已观测' if raw_row['有效计时次数'] else '分支已观测，计时不全'
        elif any(ident in outcomes[a]['candidates'] for a in anchors):
            status = '端点已见，分支待辨' if any(outcomes[a]['state'] in ('端点已见，分支待辨', '端点间可能重试') for a in anchors) else '仅有路径证据'
        else:
            status = '本次未观测'
        states[status] += 1
        info = describe(source_index, edge, status)
        basis = []
        for anchor, reason in sorted(anchors.items()):
            sample = observations[anchor]
            basis.append(dict(reason, channel=sample['控制器'], start=anchor[1], end=anchor[2],
                              recording=sample['样例采集ID'] or '1', sample_index=sample['样例跳变索引']))
        evidence[ident] = dict(info, witnesses=basis, site=edge.site)
        display = names[edge.channel]
        first = observations[next(iter(anchors))]
        row = {'分支ID': ident, '构成要素': first['构成要素'] if edge.channel in sampled else display, '控制器': display, '动作': '源码步序分支',
               '开始标签': f'{display}={edge.start}', '结束标签': f'{display}={edge.end}',
               '状态': 'action_branch', '本次状态': status, '源码关系': 'direct', '证据POU': edge.site['scope'],
               '证据节点': ident, '源码位置': location(edge.site), '原始条件': edge.site['guard'],
               '适用工况': edge.site.get('operating_guard', ''),
               '动作描述': info['description'], '动作变量': '; '.join(dict.fromkeys(c['arguments'] for c in info['linked_commands'])),
               '关联实测': '、'.join(f"{v['start']} → {v['end']}" for v in basis),
               '收录依据': '、'.join(dict.fromkeys(v['reason'] for v in basis))}
        if measured:
            row.update({k: raw_row[k] for k in TIME_FIELDS})
        elif status == '本次未观测':
            row.update({'发生次数': 0, '有效计时次数': 0})
        rows.append(row)
    rows.sort(key=lambda r: (r['控制器'].casefold(), int(r['开始标签'].rsplit('=', 1)[1]),
                             int(r['结束标签'].rsplit('=', 1)[1]), r['分支ID']))
    dump_rows(run / BRANCH_FILE, [*PAIR_FIELDS, *BRANCH_FIELDS], rows)
    comparison = []
    origins = []
    for key, row in observations.items():
        outcome = outcomes[key]
        if event_counts[key] != row['发生次数']:
            raise ValueError(f'原始跳变与状态对计数不一致 {key}')
        for ident in (*outcome['candidates'], *outcome['pending_exits']):
            edge = index.writes[ident]
            links[ident] = {'channel': names[edge.channel], 'from': edge.start, 'to': edge.end,
                            'location': location(edge.site), 'guard': edge.site['guard'],
                            **{k:edge.site[k] for k in ('scope','source_line','network','rung_order',
                               'dependencies','invocation','binding','guard_complete') if k in edge.site}}
        detail = '、'.join(dict.fromkeys(location(s) + ' ' + s.get('write_value', '') for s in outcome['unresolved']))
        pending = [index.writes[i] for i in outcome['pending_exits']]
        modes = {index.writes[i].site.get('operating_guard', '') for i in outcome['candidates']}
        signals = sorted({v for mode in modes if mode and len(modes) > 1
                          for v in _conditions(source_condition(mode), key[0])})
        origins.append(dict(channel=names[key[0]],start=key[1],end=key[2],**outcome['origin']))
        comparison.append({**row, **origin_columns(outcome['origin']), '待核写入': detail,
                           '待核出口': '、'.join(f"{e.start} → {e.end} {location(e.site)}" for e in pending),
                           '工况核对信号': '、'.join(signals), '分支判定': outcome['state'], '分支ID': outcome.get('branch', ''),
                           '候选写点': '、'.join(outcome['candidates'])})
    dump_rows(run / OBSERVATIONS_FILE, [*PAIR_FIELDS, '分支判定', '分支ID', '候选写点', '待核写入', '待核出口', '工况核对信号',
              '跳变来源','源码步序路径','所属程序段','其他写入来源'], comparison)
    (run / EVIDENCE_FILE).write_text(json.dumps({'schema': 2, 'branches': evidence, 'candidates': links,'observations':origins},
                                               ensure_ascii=False, indent=1), encoding='utf-8')
    return {'rows': len(rows), 'states': dict(states), 'observed_pairs': len(observations),
            'origins':dict(Counter(o['kind'] for o in origins)),
            'observations_by_state': dict(Counter(v['state'] for v in outcomes.values())),
            'review_pairs': sum(v['state'] != '分支已观测' or bool(v['pending_exits']) for v in outcomes.values())}
