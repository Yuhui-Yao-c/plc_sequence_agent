"""Find omitted cursors whose own steps issue and await distinct device commands."""
from collections import defaultdict, deque

from plc_trace.analysis.boundary_logic import combine, compatible
from plc_trace.analysis.fb_transport import required_signals
from plc_trace.capture.capture_use import _same_phase
from plc_trace.capture.station_scope import writer_context
from plc_trace.source.source_expressions import source_condition


def _device(command, feedback, cursor):
    """A concrete shared device instance, not a common station/namespace prefix."""
    left, right = command.split('.')[:-1], feedback.split('.')[:-1]
    shared = []
    for a, b in zip(left, right):
        if a != b:
            break
        shared.append(a)
    device = '.'.join(shared)
    if not shared or command == feedback or cursor.startswith(device + '.'):
        return None
    if len(shared) >= 2 or left == right:
        return device
    return None


def _case(site, selector):
    frames = [(block, arm) for kind, block, arm, name in site.get('frames', ())
              if kind == 'case' and name.casefold() == selector.casefold()]
    return frames[0] if len(frames) == 1 else None


def _commands_before_feedback(row, write, effect, feedback, polarity):
    # A common PLC pattern requests motion while the feedback is absent, then
    # advances once it arrives. Check that all other gates can hold together.
    guard = source_condition(effect['guard'])
    conjuncts = guard.args if guard.kind == 'and' else (guard,)
    remaining = [part for part in conjuncts if required_signals(part) != {(feedback, not polarity)}
                 or part.kind not in ('signal', 'not')]
    if len(remaining) == len(conjuncts):
        return _same_phase(row, write, effect)
    return compatible(combine('and', source_condition(write['guard']), *remaining),
                      row['identity'], write['from'], check_remaining=True)


def _linked_stages(edges, stages):
    for first in stages:
        reached, todo = set(), deque([first['to']])
        while todo:
            step = todo.popleft()
            if step in reached:
                continue
            reached.add(step)
            for other in stages:
                if (other['from'] == step and step != first['from']
                        and (other['command'], other['feedback']) != (first['command'], first['feedback'])):
                    return [first, other]
            todo.extend(edges.get(step, ()))


def action_obligation(row):
    """Evidence requiring a second look, never a claim of production by itself.

    A private cursor's intrinsic device actions cannot distinguish a fixture
    from an independent workpiece task. Closed, unexposed private cursors
    are handled by parent/consumer evidence elsewhere.
    """
    activation = row.get('activation')
    if not row['sequence_witness'] or (activation and not activation['open_lines']):
        return None
    effects = defaultdict(list)
    for effect in row['effects']:
        if effect.get('guard_complete') and effect.get('global_target') and effect.get('value') == 'TRUE':
            location = writer_context(effect), effect.get('network'), effect.get('source_line')
            effects[location, effect['step']].append(effect)
    if not effects:
        return None
    graphs, stages = defaultdict(lambda: defaultdict(set)), defaultdict(list)
    for write in row['transitions']:
        selector = write.get('case_selector')
        if (not selector or not write.get('guard_complete')
                or any(not call.get('guard_complete') for call in write.get('binding', ()))):
            continue
        arm = _case(write, selector)
        if arm is None:
            continue
        location = writer_context(write), write.get('network'), write.get('source_line')
        mode = write.get('operating_guard') or ''
        group = location, arm[0], mode
        graphs[group][write['from']].add(write['to'])
        gates = required_signals(source_condition(write['guard'])) - required_signals(source_condition(mode or 'TRUE'))
        for effect in effects[location, write['from']]:
            if _case(effect, selector) != arm:
                continue
            command = effect['global_target']
            for feedback, polarity in sorted(gates):
                if feedback not in write.get('global_reads', ()):
                    continue
                device = _device(command, feedback, row['identity'])
                if device and _commands_before_feedback(row, write, effect, feedback, polarity):
                    stages[group].append({'write': write['id'], 'effect': effect['id'],
                                          'from': write['from'], 'to': write['to'], 'device': device,
                                          'command': command, 'feedback': feedback, 'polarity': polarity})
    modes = []
    for (location, case, mode), found in stages.items():
        pair = _linked_stages(graphs[location, case, mode], found)
        if pair:
            modes.append({'scope': location[0][0], 'operating_guard': mode, 'stages': pair})
    if modes:
        return {'kind': 'device_action_stages', 'modes': modes,
                'reason': '本实例在同一运行条件下有相互接续的执行步，分别下发不同设备指令并等待相应反馈。请核对哪一通道记录这些阶段的生产起止。'}
