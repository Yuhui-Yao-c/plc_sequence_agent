"""Source contradictions to check before a production cursor is left out of the trace list."""
from collections import defaultdict
from functools import lru_cache

from plc_trace.analysis.boundary_logic import combine
from plc_trace.analysis.fb_transport import required_signals
from plc_trace.analysis.feedback_successor import implies_at
from plc_trace.capture.action_obligations import _device
from plc_trace.capture.capture_use import _same_phase
from plc_trace.capture.coverage import _job_ports
from plc_trace.capture.coordination_scope import _parked_launch
from plc_trace.capture.station_scope import writer_context
from plc_trace.source.source_expressions import source_condition


def _mode(site):
    return writer_context(site), site.get('operating_guard', '')


@lru_cache(maxsize=2048)
def _signals(condition):
    return required_signals(source_condition(condition or 'TRUE'))


def _feedback(write):
    # Only a positive, necessary BOOL input newly tested on this exit. The binder
    # has to have resolved it to this exact instance's global port, too.
    operands = _signals(write['guard']) - _signals(write.get('operating_guard', ''))
    return {port for port, high in operands if high}.intersection(write.get('global_reads', ()))


def _commands(row):
    return [e for e in row['effects'] if e.get('guard_complete') and e.get('global_target')
            and str(e.get('value')).upper() == 'TRUE']


def _issued_in_phase(row, write, effect, feedback):
    if _same_phase(row, write, effect):
        return True
    # Often the request is set while DONE is still false, and the cursor exits
    # on a later scan after DONE is true. Require the exact same CASE arm.
    def cases(site):
        return [(c, arm) for kind, c, arm, _ in site.get('frames', ()) if kind == 'case']
    return (write.get('network') == effect.get('network') and cases(write) and cases(write) == cases(effect)
            and (feedback, False) in _signals(effect['guard'])
            and implies_at(source_condition(effect['guard']), source_condition(write.get('operating_guard') or 'TRUE'),
                           row['identity'], write['from']))


def _device_phases(row, writes):
    by_step = defaultdict(list)
    for effect in _commands(row):
        by_step[writer_context(effect), effect['step']].append(effect)
    phases = {}
    for write in writes:
        for feedback in sorted(_feedback(write)):
            for command in by_step[writer_context(write), write['from']]:
                target = command['global_target']
                if _device(target, feedback, row['identity']) and _issued_in_phase(row, write, command, feedback):
                    phases[write['id']] = {'write': write['id'], 'command': command['id'], 'feedback': feedback}
                    break
            if write['id'] in phases:
                break
    return phases


def _two_stages(writes, phases, endpoint):
    """Follow predecessors, looking for two successive device stages in one mode."""
    predecessors = defaultdict(list)
    for w in writes:
        predecessors[w['to']].append(w)
    pending = [(endpoint, None)]
    seen = set()
    while pending:
        step, last = pending.pop()
        if (step, last) in seen:
            continue
        seen.add((step, last))
        for w in predecessors[step]:
            previous = last
            if w['id'] in phases:
                if last and w['from'] != last[0] and phases[w['id']]['feedback'] != phases[last[1]]['feedback']:
                    return [phases[w['id']], phases[last[1]]]
                previous = w['from'], w['id']
            if w['from'] != endpoint:
                pending.append((w['from'], previous))
    return []


def _production_consumers(rows, decisions):
    active_modes = defaultdict(set)
    waits = defaultdict(list)
    children = defaultdict(list)
    for d in decisions:
        if d['admission'] != 'captured':
            continue
        row = rows[d['name'].casefold()]
        anchors = {b['write'] for b in d['boundaries']}
        modes = {_mode(w) for w in row['transitions'] if w['id'] in anchors}
        active_modes[row['identity']] = modes
        for w in row['transitions']:
            if _mode(w) not in modes or not w.get('guard_complete'):
                continue
            peer = {'peer': row['name'], 'peer_write': w['id']}
            for feedback in sorted(_feedback(w)):
                waits[feedback].append(peer)
            links = [l for l in row['control_links'] if l['parent'].casefold() == row['identity']
                     and l['parent_step'] == w['from'] and writer_context(l) == writer_context(w)]
            for link in links:
                if link['kind'] != 'starts_child':
                    continue
                parked = _parked_launch(row, rows, {'via': link['id']}, w)
                if parked:
                    for child, start in parked['children'].items():
                        children[child.casefold()].append(dict(peer, kind='recurring_jobs', link=start))
                    break
            for link in links:
                if (link['kind'] != 'waits_for_child' or link.get('offset') != w.get('offset')
                        or link.get('network') != w.get('network') or link['guard'] != w['guard']):
                    continue
                starts = [s for s in row['control_links'] if s['kind'] == 'starts_child' and s['parent'] == row['name']
                          and s['child'] == link['child'] and writer_context(s) == writer_context(w)
                          and s.get('guard_complete') and implies_at(source_condition(s['guard']),
                                source_condition(w.get('operating_guard') or 'TRUE'), row['identity'], s['parent_step'])]
                if len({s['parent_step'] for s in starts}) >= 2:
                    children[link['child'].casefold()].append(dict(peer, kind='restarted_child', link=link['id'],
                            starts=[s['id'] for s in starts], terminal=link['child_value'], mode=_mode(w)))
    return waits, children, active_modes


def _response_path(peer, mode_writes, requests, complete, reply, anchored):
    """Trace one worker from accepting a request, through a device operation, to publishing its result."""
    request_gate = combine('or', *(source_condition(p) for p in sorted(requests)))
    phases = _device_phases(peer, mode_writes)
    if not anchored and not _two_stages(mode_writes, phases, complete['from']):
        return None
    outgoing = defaultdict(list)
    for w in mode_writes:
        outgoing[w['from']].append(w)
    for entry in mode_writes:
        if not requests.intersection(entry.get('global_reads', ())):
            continue
        if not implies_at(source_condition(entry['guard']), request_gate, peer['identity'], entry['from']):
            continue
        # A reset to the request step or publication by another branch cannot
        # substitute for an action between this instance accepting and replying.
        seen, queue = set(), [(entry['to'], None)]
        while queue:
            step, action = queue.pop()
            if step == complete['from']:
                if action:
                    return {'peer': peer['name'], 'peer_write': entry['id'], 'peer_stage': phases[action],
                            'peer_finish': complete['id'], 'peer_result': reply['id']}
                continue
            if step == entry['from'] or (step, action) in seen:
                continue
            seen.add((step, action))
            for edge in outgoing[step]:
                if edge['id'] in phases and phases[edge['id']]['feedback'] not in requests:
                    action_at = edge['id']
                else:
                    action_at = action
                queue.append((edge['to'], action_at))
    return None


def request_exchanges(row, writers):
    """Concrete command set on entry, cleared with a response from a single peer on exit."""
    writes = {w['id']: w for w in row['transitions']}
    effects = {e['id']: e for e in row['effects']}
    transactions = defaultdict(dict)
    for request, jobs in _job_ports([row]).items():
        if writers[request] != {row['identity']}:
            continue
        for _, start, command, end, _, _, complete in jobs:
            if complete:
                transactions[start, end][request] = command
    for (start, end), issued in transactions.items():
        exit_write = writes[end]
        cleared = {e.get('global_target'): e['id'] for e in effects.values()
                   if e.get('guard_complete') and e.get('value') == 'FALSE' and _same_phase(row, exit_write, e)
                   and implies_at(source_condition(exit_write['guard']), source_condition(e['guard']),
                                  row['identity'], exit_write['from'])}
        requests = issued.keys() & cleared.keys()
        if not requests:
            continue
        for result in sorted(_feedback(exit_write) & cleared.keys() - requests):
            owners = writers[result] - {row['identity']}
            if len(owners) != 1 or row['identity'] in writers[result]:
                continue
            owner, = owners
            yield start, end, result, owner, {name: issued[name] for name in sorted(requests)}, cleared


def _round_trip(row, rows, writers, available, active_modes):
    writes = {w['id']: w for w in row['transitions']}
    for start, end, result, owner, issued, cleared in request_exchanges(row, writers):
        requests = set(issued)
        if owner not in available:
            continue
        peer = rows[owner]
        # The two ends must own distinct execution programs/FB instances.
        for reply in _commands(peer):
            if reply['global_target'] != result or writer_context(reply) == writer_context(writes[end]):
                continue
            for finish in peer['transitions']:
                if not finish.get('guard_complete') or not _same_phase(peer, finish, reply):
                    continue
                mode_writes = [w for w in peer['transitions'] if w.get('guard_complete') and _mode(w) == _mode(finish)]
                anchored = _mode(finish) in active_modes[owner]
                if path := _response_path(peer, mode_writes, requests, finish, reply, anchored):
                    return dict(path, kind='work_request_and_result', write=start, wait_write=end,
                                requests={name:issued[name] for name in sorted(requests)}, result=result,
                                clear_request={name:cleared[name] for name in sorted(requests)}, clear_result=cleared[result],
                                reason='本步发起作业并等待结果；另一实例响应本次请求，执行设备指令并收到反馈后发布结果。本步随后复位请求与完成信号。')


def signal_writers(rows):
    writers = defaultdict(set)
    for row in rows.values():
        for effect in row['effects']:
            if effect.get('global_target') and str(effect.get('value')).upper() == 'TRUE':
                writers[effect['global_target']].add(row['identity'])
    return writers


def production_counterchecks(inventory, decisions):
    """Return specific counter-evidence for a second review; never infer automatic membership."""
    rows = {r['identity']: r for r in inventory['candidates']}
    waits, children, active_modes = _production_consumers(rows, decisions)
    writers = signal_writers(rows)
    available = {d['name'].casefold() for d in decisions if not d.get('exclusion_evidence')
                 and d['admission'] != 'excluded'}
    result = {}
    for d in decisions:
        row = rows[d['name'].casefold()]
        key = row['identity']
        if (d['admission'] in ('captured', 'retained', 'excluded') or d.get('exclusion_evidence')
                or not row['sequence_witness'] or row.get('service_isolation')):
            continue
        endpoints = defaultdict(list)
        published = [e for e in _commands(row) if e['global_target'] in waits and writers[e['global_target']] == {key}]
        if proof := _round_trip(row, rows, writers, available, active_modes):
            result[key] = proof
            continue
        if not published and key not in children:
            continue
        writes = [w for w in row['transitions'] if w.get('guard_complete')]
        for w in writes:
            for e in published:
                if _same_phase(row, w, e):
                    peer = next((p for p in waits[e['global_target']] if p['peer'].casefold() != key), None)
                    if peer:
                        endpoints[_mode(w)].append((w['from'], dict(peer, kind='consumed_completion',
                               publish=e['id'], result=e['global_target'])))
        for child in children[key]:
            if child['kind'] == 'recurring_jobs':
                parent = rows[child['peer'].casefold()]
                start = next(l for l in parent['control_links'] if l['id'] == child['link'])
                write = next(w for w in parent['transitions'] if w['id'] == child['peer_write'])
                endpoints[_mode(write)].append((start['child_value'], child))
            else:
                evidence = {k:v for k,v in child.items() if k != 'mode'}
                endpoints[child['mode']].append((child['terminal'], evidence))
        for mode, tails in endpoints.items():
            branches = [w for w in writes if _mode(w) == mode]
            phases = _device_phases(row, branches)
            for terminal, peer in tails:
                stages = _two_stages(branches, phases, terminal)
                if stages:
                    result[key] = dict(peer, write=stages[0]['write'], stages=stages,
                                       reason='已采生产通道与本实例有直接控制或完成反馈关系，本实例还有分阶段命令及到位写点。')
                    break
            if key in result:
                break
    return result
