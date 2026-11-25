"""Project-independent retention of concrete state cursors and verification of exclusions."""
import re
from collections import defaultdict

from plc_trace.capture.capture_use import _guard_signals, _same_phase
from plc_trace.capture.station_scope import writer_context
from plc_trace.analysis.fb_transport import required_signals
from plc_trace.source.source_expressions import source_condition


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
            if (declaration['path'].casefold() == gate and
                re.search(r'手动模式|维护模式|维修模式|调试模式|\b(?:manual|maintenance|service|commissioning) mode\b',
                          note, re.I) and not re.search(r'非|退出|禁用|屏蔽|自动|\b(?:not|exit|disabled|auto)\b', note, re.I)):
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
            if effect.get('guard_complete') and effect.get('global_target') and str(effect.get('value')).upper() == 'TRUE':
                starts[writer_context(effect), effect['step']].append(effect)
        if not starts:
            continue
        exits = defaultdict(list)
        for end in row['transitions']:
            if end.get('guard_complete') and end.get('global_reads'):
                exits[writer_context(end), end['from'], end.get('operating_guard', '')].append(end)
        for start in row['transitions']:
            owner = writer_context(start)
            for command in starts.get((owner, start['from']), ()):
                mode = start.get('operating_guard', '')
                for end in exits.get((owner, start['to'], mode), ()):
                    if not _same_phase(row, start, command):
                        continue
                    jobs[command['global_target']].append(
                        (row['name'], start['id'], command['id'], end['id'], end['global_reads'], end['guard']))
    return jobs


def _positive_operand(expr, name):
    if expr.kind == 'signal':
        return expr.args[0].casefold() == name
    return expr.kind in ('and', 'or') and any(_positive_operand(arg, name) for arg in expr.args)


def _requires_result(expr, results):
    if expr.kind == 'signal':
        return expr.args[0].casefold() in results
    if expr.kind == 'and':
        return any(_requires_result(part, results) for part in expr.args)
    if expr.kind == 'or':
        return all(_requires_result(part, results) for part in expr.args)
    return False


def _covered_transaction(row, jobs):
    """Prove a single request/result exchange with at most a final acknowledgement.
    A further intermediate stage needs its own channel, even inside the same parent job.
    """
    writes = row['transitions']
    if (not row.get('request_inputs') or row['control_links'] or not all(t.get('guard_complete') for t in writes) or
            len({(writer_context(t), t.get('operating_guard', '')) for t in writes}) != 1):
        return None
    edges = {(t['from'], t['to']) for t in writes}
    results = defaultdict(set)
    for e in row['effects']:
        if e.get('global_target') and str(e.get('value')).upper() == 'TRUE':
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
        for parent, start, command, end, reads, guard in jobs.get(request, ()):
            if parent.casefold() == row['identity']:
                continue
            for idle, wait in edges:
                if not any(t['from'] == idle and t['to'] == wait and request in t.get('global_reads', ())
                           and _positive_operand(source_condition(t['guard']), request) for t in writes):
                    continue
                completion = {p for p in results[wait].intersection(reads)
                              if p != request and p.rpartition('.')[0] == port}
                if not completion or not _requires_result(source_condition(guard), completion):
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


def protect_channels(inventory, roles, required=()):
    """Apply source obligations after review. Only positive production parents can cover a child."""
    rows = {r['identity']: r for r in inventory['candidates']}
    positive = [rows[d['name'].casefold()] for d in roles if d['admission'] == 'captured']
    jobs = _job_ports(positive)
    obligations = source_obligations(inventory)
    required = set(required)
    by_name = {d['name'].casefold(): d for d in roles}
    for key, row in rows.items():
        decision = by_name.get(key)
        if row['eligibility'] == 'distributed_write':
            reason = '同一实例有多个实际写入值，其他语句按这些值选择操作，写前值未证实'
            item = {'name': row['name'], 'role': 'uncertain', 'use': 'unresolved', 'basis': [],
                    'admission': 'retained', 'reason': reason, 'retention_reason': reason}
            # An externally assigned identifier can dispatch outputs too. Keep an
            # explicitly documented data mapping visible for review at its exact address.
            for d in row['declarations']:
                note = d.get('text', '')
                if (key not in required and d['path'].casefold() == key and
                    re.search(r'编号|编码|权限|序号|\b(?:identifier|access level|recipe number)\b', note, re.I) and
                    not re.search(r'步|步骤|阶段|\b(?:state|step|phase|sequence)\b', note, re.I)):
                    item.update(admission='review', basis=[d['id']], reason='声明为“' + note + '”，已有赋码和按值分派，尚缺按工步推进的证据')
                    item.pop('retention_reason')
                    break
            roles.append(item)
            continue
        if key in required and row['eligibility'] in ('scope_required', 'single_write'):
            obligations[key] = '独立核验输入确认了本地址，源码有实际写步'
        if not decision and row['eligibility'] == 'single_write':
            decision = {'name': row['name'], 'role': 'uncertain', 'use': 'unresolved',
                        'admission': 'review', 'basis': [row['transitions'][0]['id']],
                        'reason': '单向写入尚未核实用途，可按本实例写点复核'}
            roles.append(decision)
        if not decision or decision['admission'] == 'captured' or key not in obligations:
            continue
        # An independent reference takes precedence over every discretionary exclusion.
        proof = None
        if key not in required:
            if decision.get('use') == 'detail':
                proof = _covered_transaction(row, jobs)
            elif decision.get('use') == 'service':
                # A statically gated service can be verified against the source-mode
                # declaration. Unguarded jobs with observable I/O keep their channel.
                proof = row.get('service_isolation')
            elif decision['role'] == 'non_sequence':
                if data_reason := _data_use(row):
                    proof = {'reason': data_reason}
        if proof:
            decision['exclusion_evidence'] = proof
        else:
            decision['admission'] = 'retained'
            decision['retention_reason'] = obligations[key]
