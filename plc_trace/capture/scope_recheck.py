"""Cross-check the proposed capture list against operations observable in this project."""

from plc_trace.analysis.feedback_successor import implies_at
from plc_trace.capture.action_obligations import action_obligation
from plc_trace.capture.production_countercheck import _device_phases, _mode, _two_stages, production_counterchecks
from plc_trace.capture.staged_result import staged_result_checks
from plc_trace.capture.station_scope import writer_context
from plc_trace.source.source_expressions import required_atoms, source_condition


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


def _parallel_job(row, rows, selected, anchor):
    """A common barrier cannot timestamp separately driven jobs completing in parallel."""
    writes = [w for w in row['transitions'] if w.get('guard_complete') and _mode(w) == _mode(anchor)]
    for link in row['control_links']:
        parent = link['parent'].casefold()
        if (link['kind'] != 'waits_for_child' or link['child'].casefold() != row['identity']
                or _mode(link) not in selected.get(parent, ()) or not link.get('guard_complete')):
            continue
        peers = rows[parent]['control_links']
        joint = {other['child'].casefold() for other in peers
                 if other['kind'] == 'waits_for_child' and other['parent'].casefold() == parent
                 and all(other.get(k) == link.get(k) for k in ('scope', 'binding', 'network', 'offset', 'guard'))}
        started = {other['child'].casefold() for other in peers if other['kind'] == 'starts_child'
                   and other['parent'].casefold() == parent and other.get('guard_complete')
                   and writer_context(other) == writer_context(link) and implies_at(source_condition(other['guard']),
                       source_condition(link.get('operating_guard') or 'TRUE'), parent, other['parent_step'])}
        operands = {a.args[0].casefold() for a in required_atoms(source_condition(link['guard']), {'eq'}).values()}
        # A program-local child can appear without its program qualifier in ST.
        program = link['program'].rsplit('::', 1)[-1].casefold()
        compared = {k for k in joint & started if k in operands or
                    (k.startswith(program + '.') and k[len(program) + 1:] in operands)}
        if row['identity'] in compared and len(compared) >= 2:
            stages = _two_stages(writes, _device_phases(row, writes), link['child_value'])
            if stages:
                return {'kind': 'parallel_job', 'peer': link['parent'], 'link': link['id'], 'stages': stages}
    return None


def verify_private_boundaries(inventory, decisions, commands, required):
    """Check whether a local protocol claimed as production has an independent consumer."""
    rows = {r['identity']: r for r in inventory['candidates']}
    claims = {}
    for decision in decisions:
        key = decision['name'].casefold()
        if decision['admission'] == 'captured' and (proof := commands.private_protocol(rows[key])):
            claims[key] = proof
    if not claims:
        return
    # Let a verified station or other established operation corroborate the
    # claim. Two unverified equipment cursors cannot certify one another.
    peers = [dict(d, admission='review') if d['name'].casefold() in claims else d for d in decisions]
    selected = {}
    for peer in peers:
        if peer['admission'] == 'captured':
            name = peer['name'].casefold()
            anchors = {b['write'] for b in peer['boundaries']}
            selected[name] = {_mode(w) for w in rows[name]['transitions'] if w['id'] in anchors}
    transfers = (production_counterchecks(inventory, peers), staged_result_checks(inventory, peers))
    for decision in decisions:
        key = decision['name'].casefold()
        if key not in claims:
            continue
        row = rows[key]
        writes = {w['id']: w for w in row['transitions']}
        anchor = writes[decision['timing_basis']['boundary_write']]
        transfer = next((found[key] for found in transfers if key in found and found[key]['peer'].casefold() in selected
                         and _mode(writes[found[key]['write']]) == _mode(anchor)), None)
        if transfer:
            decision['timing_corroboration'] = transfer
        elif independent := _parallel_job(row, rows, selected, anchor):
            decision['timing_corroboration'] = independent
        else:
            proof = claims[key]
            decision.update(admission='review', effective_use='unresolved',
                            exclusion_evidence=proof, timing_review_reason=proof['reason'])
            if key in required:
                decision.update(admission='retained', effective_use='reference',
                                retention_reason='独立核验输入指定该地址，源码有本实例写步')


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
