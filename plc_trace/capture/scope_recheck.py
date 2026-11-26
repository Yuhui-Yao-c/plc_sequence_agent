"""Cross-check the proposed capture list against operations observable in this project."""

from plc_trace.capture.action_obligations import action_obligation
from plc_trace.capture.production_countercheck import production_counterchecks
from plc_trace.capture.staged_result import staged_result_checks


def _instruction(write):
    """Same executable write to the same formal member across unconditional FB calls."""
    calls = write.get('binding')
    if not calls or not write.get('guard_complete') or not write.get('source_line'):
        return None
    if any(not call.get('type') or call.get('guard') != 'TRUE'
           or call.get('guard_complete') is False for call in calls):
        return None
    # A wired MOVE has its own XML node. An ST section needs an instruction offset.
    offset = write.get('offset')
    kind = write.get('kind')
    if not kind or (offset is None and kind != 'plcopen_wired_move'):
        return None
    selector = (write.get('selector') or '').casefold()
    if not selector:
        return None
    call = calls[-1]
    names = {(actual.casefold(), formal.casefold()) for formal, actual in call.get('reference_actuals', {}).items()
             if isinstance(actual, str) and actual}
    if call.get('instance'):
        names.add((call['instance'].casefold(), '@instance'))
    matches = {formal + selector[len(actual):] for actual, formal in names
               if selector == actual or selector.startswith((actual + '.', actual + '['))}
    if len(matches) > 1:
        return None
    formal = next(iter(matches), selector)
    return (tuple(c['type'].casefold() for c in calls), kind, write['source_line'], offset,
            formal, write['from'], write['to'])


def scope_recheck(inventory, roles, required_keys=()):
    """Expose specific contradictory evidence without promoting a cursor by analogy."""
    if not roles:
        return []
    rows = {r['identity']: r for r in inventory['candidates']}
    included = {d['name'].casefold() for d in roles if d['admission'] in ('captured', 'retained')}
    included.update(required_keys)
    known_sites = {}
    owners = {}
    for decision in roles:
        row = rows[decision['name'].casefold()]
        proof = decision.get('exclusion_evidence', {})
        if proof.get('kind') == 'enclosed_child':
            owners.setdefault(proof['parent'].casefold(), []).append({
                'child': row['name'], 'start': proof['start_step'], 'wait': proof['wait_step'],
                'write': proof['parent_exit_write']})
        if decision['admission'] != 'captured':
            continue
        anchor = decision['timing_basis']['boundary_write']
        for write in row['transitions']:
            if write['id'] == anchor and (site := _instruction(write)):
                known_sites.setdefault(site, (row['name'], anchor))

    consumers = production_counterchecks(inventory, roles)
    consumers.update(staged_result_checks(inventory, roles))
    checks = []
    for decision in roles:
        key = decision['name'].casefold()
        if key in included or decision.get('exclusion_evidence'):
            continue
        row = rows[key]
        proof = consumers.get(key)
        if key in owners:
            proof = {'kind': 'missing_timing_owner', 'children': owners[key],
                     'reason': '已按该父步的启动、终值等待和接管关系合并内部子步，但此父步也没有入选。请核对哪一地址实际记录这项作业。'}
        elif not proof:
            proof = action_obligation(row)
        if not proof:
            for write in row['transitions']:
                peer = known_sites.get(_instruction(write))
                if peer:
                    proof = {'kind': 'same_bound_operation', 'write': write['id'], 'peer': peer[0], 'peer_write': peer[1],
                             'source_line': write['source_line'], 'reason': '另一已采实例以同一功能块指令和同一形参成员计时。请核实本实例的调用、作业对象和反馈。'}
                    break
        if proof:
            checks.append({'name': row['name'], **proof})
    return checks
