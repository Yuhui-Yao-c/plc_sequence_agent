"""A requested operation may return readiness before delivering a separately consumed result."""
from collections import defaultdict

from plc_trace.analysis.boundary_logic import combine, compatible
from plc_trace.analysis.feedback_successor import implies_at
from plc_trace.capture.action_obligations import _device
from plc_trace.capture.capture_use import _same_phase
from plc_trace.capture.production_countercheck import _device_phases, _feedback, _mode, _signals, request_exchanges, signal_writers
from plc_trace.source.source_expressions import source_condition


def _following(row, first):
    """Stay within this operation and operating mode; stop before a fork or another cycle."""
    outgoing = defaultdict(list)
    for w in row['transitions']:
        if w.get('guard_complete') and _mode(w) == _mode(first):
            outgoing[w['from']].append(w)
    step, seen = first['to'], {first['from']}
    while step not in seen and step in outgoing:
        seen.add(step)
        yield from outgoing[step]
        successors = {w['to'] for w in outgoing[step]}
        if len(successors) != 1:
            break
        step, = successors


def _effects(row, write, state):
    return [e for e in row['effects'] if e.get('guard_complete') and e.get('global_target')
            and e.get('value') == state and _same_phase(row, write, e)]


def _enabled_feedback(row, write, publication, resets):
    """With its enable set, this instance's result requires the device's own feedback."""
    inputs = {s for s, high in _signals(publication['guard']) if high}.intersection(write.get('global_reads', ()))
    guard = source_condition(write['guard'])
    for sensor in sorted(inputs & resets.keys()):
        if implies_at(guard, source_condition(sensor), row['identity'], write['from']):
            continue
        for enable in sorted(inputs - {sensor, publication['global_target']}):
            active = combine('and', guard, source_condition(enable))
            if (compatible(active, row['identity'], write['from'], check_remaining=True)
                    and implies_at(active, source_condition(sensor), row['identity'], write['from'])):
                yield sensor, enable


def _command_before_ready(row, accept, ready, sensor, enable):
    # The equipment and mode must match the device which later reported this result.
    # Take the exact branch through the command. Follow deterministic successors between it and readiness.
    for command_step in _following(row, accept):
        effects = [e for e in _effects(row, command_step, 'TRUE')
                   if _device(e['global_target'], sensor, row['identity']) and (enable, True) in _signals(e['guard'])]
        if effects and any(w['id'] == ready['id'] for w in _following(row, command_step)):
            return effects[0]['id']


def _worker_result(row, requests, acknowledgement, consumed):
    writes = [w for w in row['transitions'] if w.get('guard_complete')]
    accepted = [(w, _feedback(w) & requests) for w in writes if _feedback(w) & requests]
    for ready in writes:
        if not any(e['global_target'] == acknowledgement for e in _effects(row, ready, 'TRUE')):
            continue
        for done in _following(row, ready):
            for effect in _effects(row, done, 'TRUE'):
                payload = effect['global_target']
                if payload not in consumed:
                    continue
                for accept, triggers in accepted:
                    if _mode(accept) != _mode(ready):
                        continue
                    resets = {e['global_target']: e['id'] for e in _effects(row, accept, 'FALSE')
                              if any((r, True) in _signals(e['guard']) for r in triggers)}
                    for sensor, enable in _enabled_feedback(row, done, effect, resets):
                        command = _command_before_ready(row, accept, ready, sensor, enable)
                        if command:
                            return {'kind': 'staged_result_transfer', 'accepted': accept['id'], 'ready': ready['id'],
                                    'write': done['id'], 'result': effect['id'], 'payload': payload, 'enable': enable,
                                    'sensor': sensor, 'clear_sensor': resets[sensor], 'command': command,
                                    'peer_data': consumed[payload], 'request': sorted(triggers)[0],
                                    'reason': '该实例接受工位请求并返回就绪后，仍按本件使能等待设备反馈、写入结果；'
                                              '发起工位用另一个步值单独等待这份结果。'}


def staged_result_checks(inventory, decisions):
    rows = {r['identity']: r for r in inventory['candidates']}
    writers = signal_writers(rows)
    pending = {d['name'].casefold() for d in decisions if not d.get('exclusion_evidence')
               and d['admission'] not in {'captured', 'retained', 'excluded'}}
    checks = {}
    for d in decisions:
        if d['admission'] != 'captured':
            continue
        peer = rows[d['name'].casefold()]
        writes = {w['id']: w for w in peer['transitions']}
        modes = {_mode(writes[b['write']]) for b in d['boundaries']}
        for start, end, ack, owner, issued, _ in request_exchanges(peer, writers):
            if owner not in pending or owner in checks:
                continue
            following = list(_following(peer, writes[end]))
            hardware = _device_phases(peer, following)
            # When review covered a different mode, demand a real worker command and
            # feedback on this precise path between consuming readiness and the payload.
            action, payloads, peer_actions = None, {}, {}
            for w in following:
                if action or _mode(writes[start]) in modes:
                    for signal in _feedback(w) - issued.keys() - {ack}:
                        if owner in writers[signal]:
                            payloads[signal] = w['id']
                            peer_actions[w['id']] = action
                if w['id'] in hardware and len({v['to'] for v in following if v['from'] == w['from']}) == 1:
                    action = hardware[w['id']]
            if payloads and (proof := _worker_result(rows[owner], issued.keys(), ack, payloads)):
                checks[owner] = dict(proof, peer=peer['name'], peer_write=start, peer_ready=end, acknowledgement=ack)
                if action := peer_actions[proof['peer_data']]:
                    checks[owner]['peer_stage'] = action
    return checks
