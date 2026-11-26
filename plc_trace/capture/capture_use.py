"""Project-local review of which sequence instances mark production timing boundaries."""
import re
from functools import lru_cache

from plc_trace.analysis.boundary_logic import IDENT, combine, compatible, symbol
from plc_trace.analysis.feedback_successor import implies_at
from plc_trace.capture.station_scope import writer_context
from plc_trace.source.source_expressions import source_condition


USES = {'production': '生产动作', 'detail': '内部细节', 'service': '设备服务', 'unresolved': '用途待核',
        'referenced_slot': '仅固定引用'}


def _same_phase(candidate, write, effect):
    if effect['step'] != write['from'] or writer_context(effect) != writer_context(write):
        return False
    # Offsets are local to a section/network. Shared IF/CASE blocks must select the same arm.
    if (write.get('frames') or effect.get('frames')) and effect.get('network') != write.get('network'):
        return False
    exit_guard = source_condition(write.get('guard') or 'TRUE')
    effect_guard = source_condition(effect.get('guard') or 'TRUE')
    if not compatible(combine('and', exit_guard, effect_guard), candidate['identity'], write['from'], check_remaining=True):
        return False
    mode = write.get('operating_guard')
    if mode and not implies_at(effect_guard, source_condition(mode), candidate['identity'], write['from']):
        return False
    arms = {(kind, block): arm for kind, block, arm, _ in write.get('frames', ())}
    other = {(kind, block): arm for kind, block, arm, _ in effect.get('frames', ())}
    for kind, block, arm, selector in write.get('frames', ()):
        if kind == 'case' and selector == write.get('case_selector') and other.get((kind, block)) != arm:
            return False
    return all(arms.get((kind, block), arm) == arm for kind, block, arm, _ in effect.get('frames', ()))


@lru_cache(maxsize=512)
def _guard_signals(guard):
    """Recognized PLC operands. Quoted text, opaque calls and generated placeholders cannot attest feedback."""
    pending = [source_condition(guard or 'TRUE')]
    names = set()
    while pending:
        expr = pending.pop()
        if expr.kind in {'and', 'or', 'xor', 'not'}:
            pending.extend(expr.args)
        elif expr.kind not in {'opaque_cmp', 'const'}:
            names.update(v.casefold() for v in expr.args if isinstance(v, str)
                         and re.fullmatch(IDENT, v) and not v.casefold().startswith('__source_'))
    return frozenset(names)


def _supports(candidate, write, via, sites):
    if via == 'comment':
        return bool(re.search(r'[^\W\d_]', write.get('comment', '')))
    if via.startswith('signal:'):
        # A feedback in the actual exit predicate, beyond the selector and enclosing mode.
        signal = symbol(via[7:]).casefold()
        selectors = {candidate['identity'], candidate['name'].rsplit('.', 1)[-1].casefold(),
                     (write.get('case_selector') or write.get('selector') or '').casefold()}
        return signal not in selectors and signal in (
            _guard_signals(write.get('guard', '')) - _guard_signals(write.get('operating_guard', '')))
    site = sites.get(via)
    if site is None:
        return False
    if via.startswith('e'):
        return _same_phase(candidate, write, site)
    if via.startswith('d'):
        return (site['path'].casefold() == candidate['identity'] and bool(site.get('text'))
                and not site.get('array_bounds'))
    if via.startswith('l'):
        if site['parent'].casefold() == candidate['identity']:
            return (writer_context(site) == writer_context(write) and site['parent_step'] == write['from']
                    and site.get('guard') == write.get('guard'))
        # A child may be called or awaited in another POU. Keep the caller's own
        # predicate as separate evidence; its operating mode does not transfer to the child.
        if (site['child'].casefold() != candidate['identity']
                or site['child_value'] != write['from' if site['kind'] == 'starts_child' else 'to']):
            return False
        modes = combine('and', *(source_condition(s.get('operating_guard') or 'TRUE') for s in (site, write)))
        return compatible(modes, candidate['identity'], site['child_value'], check_remaining=True)
    return False


def timing_use(candidate, decision, sequence):
    """Validate the semantic review against this concrete instance, independently of hierarchy."""
    name = candidate['name']
    use = decision.get('use', 'unresolved')
    boundaries = decision.get('boundaries', [])
    if use not in ('production', 'service', 'detail', 'unresolved') or not isinstance(boundaries, list):
        raise ValueError(f'采集用途或动作边界格式有误：{name}')
    if use in ('service', 'detail') and not sequence:
        raise ValueError(f'尚无执行步证据，请将采集用途标为待核：{name}')
    if use != 'production':
        if boundaries:
            raise ValueError(f'只有生产动作可指定计时边界：{name}')
        return {'use': use, 'admission': 'review' if use == 'unresolved' else 'optional'}
    if not sequence or not boundaries:
        raise ValueError(f'默认采集须有真实执行步和具体生产动作边界：{name}')
    sites = {s['id']: s for group in ('effects', 'control_links', 'declarations') for s in candidate[group]}
    writes = {s['id']: s for s in candidate['transitions']}
    result, seen = [], set()
    for item in boundaries:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip()
                                                for k in ('write', 'action', 'via')):
            raise ValueError(f'动作边界须填写 write、action 和 via：{name}')
        wid, action, via = (item[k].strip() for k in ('write', 'action', 'via'))
        if wid in seen or wid not in writes or not _supports(candidate, writes[wid], via, sites):
            raise ValueError(f'动作边界写点重复或佐证未落在该实例的对应阶段：{name} {wid} {via}')
        seen.add(wid)
        result.append({'action': action, 'write': wid, 'via': via})
    return {'use': use, 'admission': 'captured', 'boundaries': result}


def capture_lists(inventory, decisions, slots):
    """Project the same reviewed ledger into mutually exclusive primary and optional lists."""
    by_name = {d['name'].casefold(): d for d in decisions}
    primary, optional = {}, []
    for row in inventory['candidates']:
        key = row['identity']
        decision = by_name.get(key, {})
        admission = decision.get('admission')
        if admission in ('captured', 'retained'):
            primary[key] = row
        elif key in slots:
            slot = slots[key]
            optional.append((slot['name'], USES['referenced_slot'], slot['type'], '', slot['source_line'],
                             '同一专用步序数组有固定引用，尚缺本下标控制生产动作的写步', slot['array_example']))
        elif admission != 'excluded' and (decision or row['eligibility'] == 'scope_required'):
            basis = set(decision.get('basis', ()))
            t = (next((t for t in row['transitions'] if t['id'] in basis), row['transitions'][0])
                 if row['transitions'] else row['dispatch_writes'][0]['write'])
            proof = decision.get('exclusion_evidence') or {}
            inactive = proof.get('kind') == 'inactive_from_initial'
            reason = proof.get('reason') or decision.get('reason', '待核对本实例的生产动作边界')
            if 'purpose' in proof and 'operation' in proof:
                reason += f" 用途见 XML 行 {proof['purpose']['line']}，执行指令见行 {proof['operation']['line']}。"
            optional.append((row['name'], '未定位可执行入口' if inactive else USES[decision.get('use', 'unresolved')],
                             row['type'] or '', t['scope'], t['source_line'], reason, ''))
    return primary, optional


def _support_text(via, write, references):
    if via == 'comment':
        return write['comment']
    if via.startswith('signal:'):
        return via[7:]
    site = references[via]
    if via.startswith('e'):
        return f"{site['target']} := {site['value']}" if site.get('value') else site['target']
    if via.startswith('d'):
        return site['text']
    verb = '启动' if site['kind'] == 'starts_child' else '等待'
    return f"{site['parent']}={site['parent_step']} {verb} {site['child']}={site['child_value']}（{site['scope']}）"


def capture_context(primary, decisions):
    for decision in decisions:
        if decision['admission'] == 'retained':
            row = primary[decision['name'].casefold()]
            if row.get('dispatch_writes'):
                for pair in row['dispatch_writes']:
                    site, read = pair['write'], pair['dispatch']
                    evidence = f"{decision['retention_reason']}。分派读取位于 {read['scope']}，XML {read['source_line']}"
                    if read.get('st_line'):
                        evidence += f"，段内 ST {read['st_line']}"
                    yield (row['name'], '待核，先采', site['scope'], site.get('operating_guard', ''),
                           '', pair['value'], site.get('guard', ''), site.get('guard_complete', False),
                           site['source_line'], site.get('st_line', ''), '', 'distributed_write', evidence)
                continue
            site = row['transitions'][0]
            yield (row['name'], '待核，先采', site['scope'], site.get('operating_guard', ''),
                   site['from'], site['to'], site.get('guard', ''), site.get('guard_complete', False),
                   site['source_line'], site.get('st_line', ''), site['id'], 'retained', decision['retention_reason'])
        if decision['admission'] != 'captured':
            continue
        row = primary[decision['name'].casefold()]
        by_id = {t['id']: t for t in row['transitions']}
        references = {s['id']: s for field in ('effects', 'declarations', 'control_links') for s in row[field]}
        for boundary in decision['boundaries']:
            site = by_id[boundary['write']]
            source = _support_text(boundary['via'], site, references)
            for entry in decision.get('parent_entry_conditions', ()):
                source += (f"。父步 {entry['channel']}={entry['step']} 的可见入口含 {entry['guard']}"
                           f"（{entry['declaration']}，XML 行 {entry['declaration_line']}）")
            yield (row['name'], boundary['action'], site['scope'], site.get('operating_guard', ''),
                   site['from'], site['to'], site.get('guard', ''), site.get('guard_complete', False),
                   site['source_line'], site.get('st_line', ''), site['id'], boundary['via'],
                   source)
