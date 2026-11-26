"""Recognize a private instruction sequence enclosed by a single production operation."""
from collections import defaultdict

from plc_trace.analysis.boundary_logic import combine, compatible
from plc_trace.analysis.feedback_successor import implies_at
from plc_trace.capture.capture_use import _guard_signals, _same_phase
from plc_trace.capture.station_scope import writer_context
from plc_trace.source.source_expressions import source_condition


def _linear_span(writes, first, last):
    """Follow unambiguous successors; do not infer ordering from the numeric step values."""
    outgoing = defaultdict(list)
    for write in writes:
        outgoing[write['from']].append(write)
    path, visited = [], set()
    step = first
    while step != last:
        successors = {w['to'] for w in outgoing[step]}
        if step in visited or len(successors) != 1:
            return []
        visited.add(step)
        path.extend(outgoing[step])
        step, = successors
    return path


def _read_matches(use, wait, exit_write):
    return (use.get('kind') == 'parent_exit' and use.get('value') == wait['child_value']
            and use.get('to') == exit_write['to']
            and (use.get('target') or '').casefold() == wait['selector'].casefold()
            and use.get('scope') == wait['scope'] and use.get('network') == wait['network']
            and use.get('section_line') == wait['source_line'] and use.get('write_offset') == wait['offset'])


def _accounted_dispatch(use, row, exit_write):
    operations = use.get('operations')
    if not operations:
        return False
    section = (use['section_line'], use['scope'], use['network'])
    def local(site):
        return (site.get('source_line'), site.get('scope'), site.get('network')) == section
    known = {s.get('offset') for s in [*row['transitions'], *row['effects']] if local(s)}
    owner = (exit_write.get('case_selector') or exit_write.get('selector') or '').casefold()
    owner_cases = {start for kind, start, _, var in exit_write.get('frames', ())
                   if local(exit_write) and kind == 'case' and var.casefold() == owner}
    return set(operations['writes']) <= known and all(
        owner_cases.intersection(cases) for cases in operations['caption_cases'])


def _handed_back(child, parent, child_path, resumes):
    """Each visible child command must be taken over in the parent's first post-join phase."""
    effects = child['effects']
    if (not resumes or not effects or child.get('other_outputs')
            or any(not effect.get('global_target') for effect in effects)):
        return []
    # A structured device can use different command fields for its two motions.
    # Match siblings only at a concrete nested device with its own feedback,
    # never two arbitrary fields under a whole station or the global namespace.
    by_device = defaultdict(set)
    for effect in effects:
        target = effect['global_target']
        by_device[target.rpartition('.')[0]].add(target)
    handbacks = set()
    for effect in effects:
        target = effect['global_target']
        device = target.rpartition('.')[0]
        driven = [w for w in child_path if _same_phase(child, w, effect)]
        if not driven:
            return []
        device_feedback = ('.' in device and len(by_device[device]) == 1
                           and all(any(s.rpartition('.')[0] == device for s in w.get('global_reads', ()))
                                   for w in driven))
        for write in resumes:
            ports = set()
            for command in parent['effects']:
                port = command.get('global_target')
                if not port or not _same_phase(parent, write, command):
                    continue
                if port == target and effect['value'].upper() != command['value'].upper():
                    ports.add(port)
                elif (device_feedback and port != target and port.rpartition('.')[0] == device
                      and any(s.rpartition('.')[0] == device for s in write.get('global_reads', ()))):
                    ports.add(port)
            if len(ports) != 1:
                return []
            handbacks.add((target, ports.pop()))
    return [{'child_output': c, 'parent_output': p} for c, p in sorted(handbacks)]


def enclosed_child(row, rows, launches):
    """Prove that a private cursor's only observed consumer joins its enclosing operation.

    Activation is available only for a concrete, unexposed private scalar. Compare actual
    sites, calls and operating modes to avoid connecting identical step numbers on another
    branch or instance. Missing evidence leaves the semantic review in effect.
    """
    activation = row.get('activation', {})
    uses = activation.get('read_uses', ())
    if not launches or activation.get('open_lines') or not uses:
        return None
    links = row['control_links']
    if not links or any(link['child'].casefold() != row['identity'] for link in links):
        return None
    starts = [link for link in links if link['kind'] == 'starts_child']
    waits = [link for link in links if link['kind'] == 'waits_for_child']
    if len(waits) != 1 or {s['id'] for s in starts} != {s['id'] for s in launches}:
        return None
    wait, = waits
    parent = rows.get(wait['parent'].casefold())
    start_steps = {(s['parent_step'], s['child_value']) for s in starts}
    if parent is None or len(start_steps) != 1 or any(s['parent'].casefold() != parent['identity'] for s in starts):
        return None
    start_step, entry = start_steps.pop()
    wait_step, terminal = wait['parent_step'], wait['child_value']
    context = writer_context(wait)
    writes = row['transitions']
    if not context[1] or any(writer_context(s) != context for s in [*starts, *writes]):
        return None

    # The enclosing operation reads a terminal once. Intermediate step values belong
    # solely to this private dispatch. Extra readers, branches or re-entry stay visible.
    child_path = _linear_span(writes, entry, terminal)
    if not child_path or len(child_path) != len(writes):
        return None
    mode = source_condition(wait.get('operating_guard') or 'TRUE')
    compatible_writes = [w for w in parent['transitions'] if compatible(
        combine('and', mode, source_condition(w.get('operating_guard') or 'TRUE')),
        parent['identity'], w['from'], check_remaining=True)]
    parent_path = _linear_span(compatible_writes, start_step, wait_step)
    exits = [w for w in compatible_writes if w['from'] == wait_step]
    if (len({w['from'] for w in parent_path}) < 2 or len(exits) != 1
            or any(writer_context(w) != context for w in [*parent_path, *exits])
            or not all(implies_at(source_condition(s['guard']), mode, parent['identity'], start_step) for s in starts)):
        return None
    exit_write, = exits
    if (wait.get('offset') is None or wait.get('source_line') is None or not wait.get('selector')
            or (exit_write.get('offset'), exit_write.get('source_line')) != (wait['offset'], wait['source_line'])):
        return None
    reads = [use for use in uses if use['kind'] != 'dispatch']
    if not reads or not all(_read_matches(use, wait, exit_write) for use in reads):
        return None
    dispatches = [use for use in uses if use['kind'] == 'dispatch']
    if not dispatches or not all(_accounted_dispatch(use, row, exit_write) for use in dispatches):
        return None

    span_steps = {w['from'] for w in parent_path} | {wait_step}
    if any(link['child'].casefold() != row['identity'] and link['parent'].casefold() == parent['identity']
           and link['parent_step'] in span_steps for link in parent['control_links']):
        return None
    mode_inputs = _guard_signals(wait.get('operating_guard') or 'TRUE') | {parent['identity']}
    if not any(set(w.get('global_reads', ())) - mode_inputs and _same_phase(parent, w, effect)
               for w in parent_path for effect in parent['effects']):
        return None  # The enclosing cursor may simply launch and monitor the actual work.
    resumes = [w for w in compatible_writes if w['from'] == exit_write['to']]
    if any(writer_context(w) != context for w in resumes):
        return None
    handbacks = _handed_back(row, parent, child_path, resumes)
    if not handbacks:
        return None  # The child can publish a separate workpiece result without exposing its cursor.
    return {'kind': 'enclosed_child', 'parent': parent['name'], 'start_step': start_step, 'wait_step': wait_step,
            'start_links': [s['id'] for s in starts], 'wait_links': [wait['id']],
            'parent_writes': [w['id'] for w in [*parent_path, exit_write]], 'parent_exit_write': exit_write['id'],
            'child_writes': [w['id'] for w in child_path], 'handbacks': handbacks,
            'reason': (f"{parent['name']} 在 {start_step} 启动本实例并继续执行自身工步，在 {wait_step} "
                       f"读取终值 {terminal} 后进入 {exit_write['to']} 并接管子步所用的执行器。"
                       "本实例的中间步值仅用于功能块内部分派。")}
