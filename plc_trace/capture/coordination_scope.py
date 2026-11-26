"""Identify reviewed write sites which establish coordination only, not a separate job."""
import re

from plc_trace.analysis.boundary_logic import TRUE, combine, integer, substitute
from plc_trace.capture.station_scope import required_cursors, writer_context
from plc_trace.source.source_expressions import source_condition


def sole_cursor_section(parser):
    """A completely parsed ST section consists solely of guarded literal cursor writes."""
    if parser.skipped or parser.i != len(parser.ts) or not parser.statements:
        return None
    # A call embedded in a predicate can also issue an action. The parser tracks
    # standalone calls separately; for this strict proof decline parentheses entirely.
    if '(' in parser.ts:
        return None
    lhs = {name.casefold() for name, *_ in parser.statements}
    if len(lhs) == 1:
        try:
            if all(integer(rhs) is not None for _, rhs, *_ in parser.statements):
                return lhs.pop()
        except ValueError:
            pass
    return None


def _same_arm(link, write):
    return (writer_context(link) == writer_context(write)
            and link.get('network') == write.get('network') and link.get('frames') == write.get('frames')
            and link['parent_step'] == write['from'] and link['guard'] == write['guard'])


def _indexed_feedback(expr):
    """A conjunction of different constant elements of one actual feedback array."""
    parts = expr.args if expr.kind == 'and' else (expr,)
    paths = [str(p.args[0]).casefold() for p in parts if p.kind == 'signal']
    if len(set(paths)) != len(parts) or len(paths) < 2:
        return None
    matches = [re.fullmatch(r'(.+)\[(-?\d+)\]', path) for path in paths]
    return sorted(paths) if all(matches) and len({m[1] for m in matches}) == 1 else None


def arrival_only(row, rows):
    """A passive readiness cursor, sampled by a multi-party AND at one parent write."""
    writes, links = row['transitions'], row['control_links']
    if (not writes or row['effects'] or len(links) != 2
            or not all(w.get('self_only_st') and w.get('guard_complete') for w in writes)):
        return None
    # A terminal comparison can trigger an actuator, timer or data handoff
    # without writing another cursor. Account for every direct source read.
    cases = {(w['scope'], w.get('network'), w['source_line'], f[1]) for w in writes
             for f in w.get('frames', ()) if f[0] == 'case' and f[3].casefold() == row['identity']}
    if not cases or any(w.get('direct_cursor_reads') != len(cases) + 1 for w in writes):
        return None
    starts = [l for l in links if l['kind'] == 'starts_child']
    ends = [l for l in links if l['kind'] == 'waits_for_child']
    if len(starts) != 1 or len(ends) != 1:
        return None
    start, end = starts[0], ends[0]
    if (start['child'].casefold() != row['identity'] or start['child'] != end['child']
            or start['parent'] != end['parent'] or writer_context(start) != writer_context(end)):
        return None
    parent = rows.get(start['parent'].casefold())
    if not parent:
        return None
    siblings = {l['child'] for l in parent['control_links'] if l['kind'] == 'waits_for_child'
                and l['guard'] == end['guard'] and writer_context(l) == writer_context(end)
                and l.get('offset') == end.get('offset') and l.get('network') == end.get('network')}
    compared = required_cursors(source_condition(end['guard']))
    if len(siblings) < 2 or any(s.casefold() not in compared for s in siblings):
        return None
    # No loops, branches or other work. After a single arrival test the cursor
    # may pass through unconditional bookkeeping states on its way to 'done'.
    outgoing = {w['from']: w for w in writes}
    if len(outgoing) != len(writes):
        return None
    step = start['child_value']
    feedback = None
    while step in outgoing:
        w = outgoing.pop(step)
        selector = w.get('case_selector') or row['identity']
        guard = substitute(source_condition(w['guard']), selector, step)
        operating = source_condition(w.get('operating_guard') or 'TRUE')
        if guard != operating:
            if feedback is not None or guard.kind != 'and':
                return None
            # Remove only conjuncts of the enclosing mode, keeping the actual exit test.
            mode = set(operating.args if operating.kind == 'and' else () if operating == TRUE else (operating,))
            parts = set(guard.args)
            if not mode.issubset(parts):
                return None
            feedback = _indexed_feedback(combine('and', *(parts - mode)))
            if not feedback:
                return None
        step = w['to']
    if outgoing or step != end['child_value'] or not feedback:
        return None
    return {'kind':'joined_readiness', 'parent':parent['name'],
            'start_link':start['id'], 'join_link':end['id'], 'feedback':feedback, 'joined_channels':sorted(siblings),
            'reason':'该通道只汇总同组到位反馈，所在程序段仅写本步值；上层与其他通道一起读取其完成态。独立工序计时依据待核。'}


def _in_mode(effect, mode):
    # For an output assignment the CASE selector is the channel, not the output.
    # Its enclosing mode is included in the complete instruction guard.
    def conjuncts(text):
        expr = source_condition(text or 'TRUE')
        return {p.key() for p in (expr.args if expr.kind == 'and' else () if expr == TRUE else (expr,))}
    return conjuncts(mode).issubset(conjuncts(effect['guard']))


def _repeats_work(child, initial, launch, mode):
    """The established child can do a full productive cycle without relaunch."""
    parent = launch['parent'].casefold()
    edges = [w for w in child['transitions'] if writer_context(w) == writer_context(launch)
             and w.get('guard_complete') and w.get('operating_guard') == mode and parent not in w.get('global_reads', ())]
    work = {e['step'] for e in child['effects'] if writer_context(e) == writer_context(launch)
            and e.get('guard_complete') and _in_mode(e, mode)
            and e.get('global_target') and str(e.get('value')).upper() == 'TRUE'}
    active = [(initial, False)]
    seen = set()
    while active:
        src, worked = active.pop()
        if (src, worked) in seen:
            continue
        seen.add((src, worked))
        for w in edges:
            if w['from'] == src:
                sent = worked or src in work
                if w['to'] == initial and sent:
                    return True
                active.append((w['to'], sent))
    return False


def _parked_launch(row, rows, boundary, write):
    if (not write.get('guard_complete') or not write.get('case_selector')
            or write.get('independent_entry') or row.get('entry_writes') or any(l['child'].casefold() == row['identity'] for l in row['control_links'])):
        return None
    own = row['identity']
    mode = write.get('operating_guard', '')
    if substitute(source_condition(write['guard']), write['case_selector'], write['from']) != source_condition(mode or 'TRUE'):
        return None
    if any(w['from'] == write['to'] and w.get('operating_guard') == mode for w in row['transitions']):
        return None
    if any(e['step'] in {write['from'], write['to']} and writer_context(e) == writer_context(write)
           and _in_mode(e, mode) and e.get('global_target') and str(e.get('value')).upper() == 'TRUE'
           for e in row['effects']):
        return None
    links = [l for l in row['control_links'] if l['kind'] == 'starts_child' and l['parent'].casefold() == own
             and _same_arm(l, write) and l.get('guard_complete')]
    # A step comment may describe a separate command or call in the same arm.
    # This proof challenges only a boundary explicitly justified by a launch.
    if boundary['via'] not in {l['id'] for l in links}:
        return None
    recurring = {l['child']:l['id'] for l in links if (child := rows.get(l['child'].casefold()))
                 and _repeats_work(child, l['child_value'], l, mode)}
    if len(recurring) < 2 or len(recurring) != len({l['child'] for l in links}):
        return None
    return {'kind':'launches_recurring_jobs', 'write':write['id'],
            'children':recurring, 'parked_step':write['to'],
            'reason':'所报写点同时启动多个可自行循环作业的子流程，之后在同工况未见本通道继续推进。逐件作业的计时依据待核。'}


def coordination_only(row, rows, boundaries):
    """Do not overturn a separate verified operation in another branch or mode."""
    if not boundaries:
        return None
    barrier = arrival_only(row, rows)
    if barrier:
        return barrier
    writes = {w['id']:w for w in row['transitions']}
    evidence = [_parked_launch(row, rows, b, writes[b['write']]) for b in boundaries]
    if all(evidence):
        return evidence[0]
    return None
