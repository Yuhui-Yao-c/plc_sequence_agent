"""Source evidence for broad discovery and review of production timing channels."""
import re
from collections import defaultdict

from plc_trace.capture.capture_use import _guard_signals, _same_phase
from plc_trace.capture.coordination_scope import coordination_only
from plc_trace.capture.production_ownership import enclosed_child
from plc_trace.capture.station_scope import writer_context
from plc_trace.analysis.boundary_logic import substitute
from plc_trace.analysis.fb_transport import required_signals
from plc_trace.source.source_expressions import source_condition


def _service_mode(note):
    # A positive mode selector can be documented with an explicit 0/1 legend.
    # Reject ambiguous or inverted descriptions instead of interpreting the signal name.
    legend = re.search(r'0\s*[:：=是]?\s*(?:屏蔽|禁用|关闭)[\s,，、/]*1\s*[:：=是]?\s*(?:启用|使能|开启)', note)
    label = note[:legend.start()] if legend else note
    return bool(re.search(r'手动模式|维护模式|维修模式|调试模式|标定模式|'
                          r'\b(?:manual|maintenance|service|commissioning|calibration) mode\b', label, re.I)
                and not re.search(r'非|退出|禁用|关闭|屏蔽|自动|未|'
                                  r'\b(?:not|exit|disabled|auto|off)\b', label, re.I))


def service_isolation(row, declarations):
    """Every writer must require the same positively enabled, documented service mode."""
    gates = None
    for write in row.sites.values():
        if not write.get('guard_complete') or not write.get('operating_guard'):
            return None
        positive = {s for s, on in required_signals(source_condition(write['operating_guard'])) if on}
        positive.intersection_update(write.get('global_reads', ()))
        gates = positive if gates is None else gates & positive
        if not gates:
            return None
    for gate in sorted(gates or ()):
        for declaration in declarations(gate):
            note = declaration.get('text', '')
            if declaration['path'].casefold() == gate and _service_mode(note):
                return {'guard': gate, 'line': declaration['line'], 'declaration': declaration['text']}
    return None


def array_storage(row):
    match = re.fullmatch(r'(.+)\[(-?\d+(?:,-?\d+)*)\]', row['identity'])
    if match:
        for decl in row['declarations']:
            if decl.get('array_bounds') and decl['path'].casefold() == row['identity']:
                return match[1], decl['line']


def source_obligations(inventory):
    """A lone write needs a second indication of its use. Untyped reservations do not qualify."""
    rows = inventory['candidates']
    arrays = {storage for row in rows if row['sequence_witness'] and (storage := array_storage(row))}
    obligations = {}
    for row in rows:
        if row['eligibility'] not in ('scope_required', 'single_write'):
            continue
        if row['sequence_witness']:
            obligations[row['identity']] = '源码有本实例的状态分派和写回'
        elif row['control_links'] or row['effects']:
            obligations[row['identity']] = '本实例的写步与受控操作或其他执行步关联'
        elif row.get('entry_writes'):
            obligations[row['identity']] = '另一处受条件控制的写入为本实例设置了该短流程的起始步'
        elif array_storage(row) in arrays:
            obligations[row['identity']] = '同一声明及具体数组有执行步，本下标也有独立写回'
        else:
            # Declaration text is corroboration, never a selector on the variable name.
            own_note = any(re.search(r'步序|步骤|工步|步进程序|自动步|顺序控制|\b(?:step|sequence|phase|state machine)\b',
                                     d.get('text', ''), re.I) for d in row['declarations']
                           if d['path'].casefold() == row['identity'])
            for write in row['transitions']:
                selector = (write.get('selector') or row['name']).casefold()
                numbered_note = re.match(rf"\s*{write['from']}\s*[:：]", write.get('comment') or '')
                if (own_note or numbered_note) and (
                        _guard_signals(write.get('guard', '')) - {selector, row['identity']}):
                    obligations[row['identity']] = '单向写步有独立条件和本变量声明或步骤说明'
                    break
    return obligations


def _job_ports(rows):
    """Index issued requests followed by a result-dependent exit of the same job and mode."""
    jobs = defaultdict(list)
    for row in rows:
        starts = defaultdict(list)
        for effect in row['effects']:
            if effect.get('global_target') and str(effect.get('value')).upper() == 'TRUE':
                starts[writer_context(effect), effect['step']].append(effect)
        if not starts:
            continue
        exits = defaultdict(list)
        for end in row['transitions']:
            if end.get('global_reads'):
                exits[writer_context(end), end['from'], end.get('operating_guard', '')].append(end)
        for start in row['transitions']:
            owner = writer_context(start)
            for command in starts.get((owner, start['from']), ()):
                mode = start.get('operating_guard', '')
                for end in exits.get((owner, start['to'], mode), ()):
                    if not _same_phase(row, start, command):
                        continue
                    selector = end.get('selector') or end.get('case_selector') or row['identity']
                    feedback = substitute(source_condition(end['guard']), selector, end['from'])
                    complete = all(s.get('guard_complete') for s in (start, command, end))
                    jobs[command['global_target']].append(
                        (row['name'], start['id'], command['id'], end['id'], end['global_reads'], feedback, complete))
    return jobs


def _positive_operand(expr, name):
    if expr.kind == 'signal':
        return expr.args[0].casefold() == name
    return expr.kind in ('and', 'or') and any(_positive_operand(arg, name) for arg in expr.args)


def _requires_result(expr, results):
    """Every remaining exit operand must be a completion published by this device."""
    if expr.kind == 'signal':
        return expr.args[0].casefold() in results
    if expr.kind in ('and', 'or'):
        return all(_requires_result(part, results) for part in expr.args)
    return False


def _job_observations(row, jobs):
    """A workpiece can wait for several devices. Its interval describes only the group."""
    edges = {(w['from'],w['to']) for w in row['transitions']}
    if len(edges) not in (2,3) or row['control_links']:
        return []
    outputs = {e['global_target'] for e in row['effects'] if e.get('global_target') and
               str(e.get('value')).upper()=='TRUE'}
    observations = []
    for request in row.get('request_inputs',()):
        signals = {p for p in outputs if p != request and p.rpartition('.')[0] == request.rpartition('.')[0]}
        for parent, start, command, end, reads, guard, _ in jobs.get(request, ()):
            if parent.casefold()==row['identity']:
                continue
            results = sorted(p for p in signals.intersection(reads) if _positive_operand(guard,p))
            if results:
                observations.append({'channel':parent,'request':request,'results':results,
                                     'start_write':start,'request_effect':command,'result_write':end})
    return observations


def _covered_transaction(row, jobs):
    """Prove a single request/result exchange with at most a final acknowledgement.
    A further intermediate stage prevents equating the two intervals.
    """
    writes = row['transitions']
    if (not row.get('request_inputs') or row['control_links'] or not all(t.get('guard_complete') for t in writes) or
            len({(writer_context(t), t.get('operating_guard', '')) for t in writes}) != 1):
        return None
    edges = {(t['from'], t['to']) for t in writes}
    results = defaultdict(set)
    for e in row['effects']:
        if e.get('guard_complete') and e.get('global_target') and str(e.get('value')).upper() == 'TRUE':
            results[e['step']].add(e['global_target'])
    for request in row['request_inputs']:
        port, sep, _ = request.rpartition('.')
        if not sep:
            continue
        # Anything this cursor commands on another concrete interface needs its
        # own disposition, even when it also happens to publish a "done" flag.
        if (any(e.get('global_target') and not e['global_target'].startswith(port + '.') for e in row['effects'])
                or any(not p.startswith(port + '.') for p in row.get('other_outputs', ()))):
            continue
        for parent, start, command, end, reads, guard, complete in jobs.get(request, ()):
            if not complete or parent.casefold() == row['identity']:
                continue
            for idle, wait in edges:
                launches = [t for t in writes if t['from'] == idle and t['to'] == wait]
                if not all(request in t.get('global_reads', ()) and
                           (request, True) in required_signals(source_condition(t['guard'])) for t in launches):
                    continue
                completion = {p for p in results[wait].intersection(reads)
                              if p != request and p.rpartition('.')[0] == port}
                if not completion or not _requires_result(guard, completion):
                    continue
                # The result stage has exactly one successor, regardless of response code.
                after = {dst for src, dst in edges if src == wait}
                if len(after) != 1:
                    continue
                final, = after
                expected = {(idle, wait), (wait, final)}
                if final != idle:
                    expected.add((final, idle))
                if edges == expected:
                    return {'channel': parent, 'request': request, 'result': min(completion),
                            'start_write': start, 'request_effect': command, 'result_write': end}
    return None


def _data_use(row):
    """Verify a reviewed data channel using its own reads, writes and declarations."""
    if row['control_links'] or not row['transitions']:
        return None
    key = row['identity']
    notes = [d.get('text', '') for d in row['declarations'] if d['path'].casefold() == key]
    writes, effects = row['transitions'], row['effects']
    if not effects and any(re.search(r'计数|配方|产品编码|结果码|检测结果|累计|\b(?:counter|recipe|result code)\b', n, re.I)
                           for n in notes):
        return '本变量声明指明数据用途，写点没有独立受控动作'
    if len(writes) != 1 or row['sequence_witness'] or not effects:
        return None
    write = writes[0]
    if _guard_signals(write['guard']) - {key, (write.get('selector') or row['name']).casefold()}:
        return None
    if write['to'] == 0:
        def increments(effect):
            target = re.escape(re.sub(r'\s+', '', effect['target']))
            value = re.sub(r'\s+', '', str(effect.get('value', '')))
            return re.fullmatch(target + r'\+[1-9][0-9]*', value, re.I)
        if all(increments(e) for e in effects):
            return '到达阈值后清零，全部受控写出均为计数器自增'
        if (all(str(e.get('value')).upper() in ('TRUE', 'FALSE') for e in effects)
                and any(re.search(r'应答|回复码|(?:给|发至|接收|通信|通讯).*数据|\b(?:acknowledg(?:e)?ment|reply code|received data)\b',
                                  n, re.I) for n in notes)):
            return '收到固定数据后设置应答信号并清零'
    if (write['from'] == 0 and all(re.fullmatch(r'[-+]?\d+', str(e.get('value'))) for e in effects)
            and any(re.search(r'设定|设定值|阈值|\b(?:threshold|setpoint)\b', n, re.I) for n in notes)):
        return '声明为设定值，零值分支统一写入数值常量'
    return None


def _inactive(row):
    state = row.get('activation', {})
    if state.get('reset_lines') and not state['open_lines'] and not state['entries']:
        return {'kind':'inactive_from_initial', 'initial':state['initial'], 'reset_lines':state['reset_lines'],
                'reason':f"私有整数从声明初值 {state['initial']} 出发，全部可执行写入均不能离开该值。"
                         + '同值写入位于 XML 行 ' + '、'.join(str(line) for line in state['reset_lines'])}


def _parent_entries(row):
    """Return the exact parent writes that account for every visible launch."""
    act = row.get('activation', {})
    if act.get('open_lines') or not act.get('entries'):
        return []
    starts = [s for s in row['control_links'] if s['kind']=='starts_child' and s['child'].casefold()==row['identity']]
    contexts = {writer_context(site)[1] for site in row.get('transitions', ())}
    parents = []
    for entry in act['entries']:
        if 'read_uses' in act and not all(k in entry for k in ('scope', 'network')):
            return []  # No shared instruction coordinate for this representation.
        links = [s for s in starts if s['child_value'] == entry['value']
                 and s.get('local_guard', s['guard']) == entry['guard']
                 and s.get('source_line') == entry['section_line'] and s.get('offset') == entry['offset']
                 and all(s.get(k) == entry[k] for k in ('scope', 'network') if k in entry)]
        if not links or (contexts and {writer_context(s)[1] for s in links} != contexts):
            return []
        parents.extend(links)
    return parents


def _parent_context(row, rows, execution):
    launches = _parent_entries(row)
    missing, modes, inspected, only_service = set(), [], False, True
    for link in launches:
        value = link['parent_step']
        parent = rows.get(link['parent'].casefold())
        if parent is None:
            missing.add(f"{link['parent']}={value}")
            continue
        incoming = [s for s in parent['transitions'] if s['to'] == value]
        if (value == parent.get('activation', {}).get('initial', 0)
                or any(s['value'] == value for s in parent.get('entry_writes', ()))):
            return None, []
        if not incoming:
            missing.add(f"{link['parent']}={value}")
            continue
        signals = set.intersection(*(required_signals(source_condition(s['guard'])) for s in incoming))
        gated = False
        for signal, positive in sorted(signals):
            if not positive or not execution:
                continue
            for decl in execution.declarations(signal):
                if decl['path'].casefold() != signal or not decl.get('text'):
                    continue
                modes.append({'channel': parent['name'], 'step': value, 'guard': signal,
                              'declaration': decl['text'], 'declaration_line': decl['line'],
                              'incoming': [s['id'] for s in incoming]})
                gated |= _service_mode(decl['text']) and all(s.get('guard_complete') for s in incoming)
        inspected = True
        only_service &= gated
    if inspected and not only_service:
        return None, modes
    if missing:
        return '启动依赖父步 ' + '、'.join(sorted(missing)) + '，其写入来源尚未定位', modes
    if inspected:
        return '所有已定位启动入口均受有工程声明的手动、维护或标定模式约束', modes
    return None, modes


def protect_channels(inventory, roles, required=(), *, execution=None):
    """Check reviewed dispositions. Only explicitly requested channels bypass timing review."""
    rows = {r['identity']: r for r in inventory['candidates']}
    by_name = {d['name'].casefold(): d for d in roles}
    required = set(required)
    for key, row in rows.items():
        if key in by_name:
            continue
        if row['eligibility'] == 'distributed_write':
            basis, reason = [], '本实例有分段写入和按值分派，写前步值及生产计时归属待核'
        elif row['eligibility'] == 'single_write':
            basis = [row['transitions'][0]['id']]
            reason = '本实例有单向写步，生产计时归属待核'
        else:
            continue
        decision = {'name': row['name'], 'role': 'uncertain', 'use': 'unresolved',
                    'admission': 'review', 'basis': basis, 'reason': reason}
        roles.append(decision)
        by_name[key] = decision

    for key, decision in by_name.items():
        row = rows[key]
        inactive = _inactive(row)
        if inactive:
            decision.update(admission='optional', exclusion_evidence=inactive)
        elif decision.get('use') in ('production', 'detail') and (
                ownership := enclosed_child(row, rows, _parent_entries(row))):
            decision.update(admission='optional', exclusion_evidence=ownership)
        elif decision['admission'] == 'captured':
            if row.get('service_isolation'):
                decision.update(admission='review', exclusion_evidence=row['service_isolation'],
                                timing_review_reason='该地址的写步受已声明的设备服务模式约束')
            elif evidence := coordination_only(row, rows, decision['boundaries']):
                decision.update(admission='review', exclusion_evidence=evidence)
            else:
                reason, modes = _parent_context(row, rows, execution)
                if modes:
                    decision['parent_entry_conditions'] = modes
                if reason:
                    decision.update(admission='review', timing_review_reason=reason)

        if key in required and decision['admission'] != 'captured':
            decision.update(admission='retained', retention_reason='独立核验输入指定该地址，源码有本实例写步')
            if inactive:
                decision['activation_conflict'] = inactive
                decision['retention_reason'] += '，但未定位离开初值的入口'

    jobs = _job_ports([rows[key] for key, d in by_name.items() if d['admission'] == 'captured'])
    request_writers = defaultdict(set)
    for row in rows.values():
        for effect in row.get('effects', ()):
            if effect.get('global_target') and str(effect.get('value')).upper() == 'TRUE':
                request_writers[effect['global_target']].add(row['identity'])

    for key, decision in by_name.items():
        if decision['admission'] in ('captured', 'retained') or decision.get('exclusion_evidence'):
            continue
        row = rows[key]
        proof = None
        if decision.get('use') == 'detail':
            if observations := _job_observations(row, jobs):
                decision['parent_timing'] = observations
            proof = _covered_transaction(row, jobs)
        elif decision.get('use') == 'service':
            proof = row.get('service_isolation')
            claim = decision.get('service_evidence') or {}
            other = any(request_writers[p] - {key} for p in row.get('request_inputs', ()))
            if not proof and execution and (claim.get('kind') == 'mode' or
                                            decision['role'] == 'auxiliary_sequence' and not other):
                proof = execution.cited_service(row, claim)
            if not proof:
                decision.update(admission='review', timing_review_reason='设备服务用途尚未由本实例入口及执行指令证实')
        elif decision['role'] == 'non_sequence':
            if reason := _data_use(row):
                proof = {'reason': reason}
            else:
                decision.update(admission='review', timing_review_reason='写步已有来源，仍缺本实例的数据用途证据')
        if proof:
            decision['exclusion_evidence'] = proof

    # Keep the review input replayable. The effective use describes what the
    # final files actually do with the channel after applying source evidence.
    by_admission = {'captured': 'production', 'retained': 'reference',
                    'review': 'unresolved', 'excluded': 'non_sequence'}
    for decision in roles:
        admission = decision['admission']
        if admission in by_admission:
            decision['effective_use'] = by_admission[admission]
        elif decision.get('exclusion_evidence', {}).get('kind') == 'inactive_from_initial':
            decision['effective_use'] = 'inactive'
        else:
            decision['effective_use'] = 'service' if decision.get('use') == 'service' else 'detail'
