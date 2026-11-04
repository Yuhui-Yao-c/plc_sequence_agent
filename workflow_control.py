"""Resumable execution checks around the v9.25 engine; no extraction rules.

The existing finish command stays compatible. New runs bind results to their
input bytes; complete additionally checks both batch families and delivery.
Status is read-only and never opens manual/GT contents.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

VERSION = '9.25.1'
CONTRACT = 'workflow_execution.json'
COMPLETION = 'workflow_completion.json'
SCHEMA = 'plc.workflow_execution.v1'
BASE_SKILLS = [
    'plc-boundary-normalization', 'plc-signal-tail-merge',
    'plc-cross-fb-transport', 'plc-feedback-successor', 'plc-station-identity',
]
FROZEN_PREFIXES = (
    'prediction_', 'sequence_candidates', 'indexed_', 'source_', 'recall_',
    'event_graph', 'plcopen_', 'cross_fb_', 'signal_merge_', 'native_', 'smc2_',
    'candidate_audit', 'filter_audit', 'candidate_process_', 'process_focus_',
    'state_boundary_', 'state_seed_', 'timing_relations', 'variable_documentation',
    'network_comments', 'step_comments', 'station_identity', 'action_desc_a',
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1048576), b''):
            h.update(data)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def local_file(run, relative):
    """A batch path must remain inside its own run directory."""
    if not isinstance(relative, str) or not relative:
        raise ValueError('批次文件路径为空或类型错误')
    path = (run / relative).resolve()
    if Path(relative).is_absolute() or run not in path.parents:
        raise ValueError('批次路径必须位于当前运行目录: ' + relative)
    return path


def batch_plan(run, manifest):
    batches = []
    for batch in manifest.get('batches', []):
        batches.append({'kind': 'process', 'batch_id': batch['batch_id'],
                        'input': batch['input'], 'output': batch['output']})
    for path in sorted((run / 'batches_desc').glob('*.input.json')):
        data = read_json(path)
        batches.append({'kind': 'description', 'batch_id': data['batch_id'],
                        'input': path.relative_to(run).as_posix(),
                        'output': path.relative_to(run).as_posix().replace('.input.json', '.result.json')})
    return batches


def seal_run(out):
    """Add execution metadata without modifying any engine-produced artifact."""
    run = Path(out).resolve()
    manifest = read_json(run / 'workflow_manifest.json')
    batches = batch_plan(run, manifest)
    for batch in batches:
        batch['input_sha256'] = digest(local_file(run, batch['input']))
    frozen = {p.name: digest(p) for p in sorted(run.iterdir()) if p.is_file()
              and (p.name.startswith(FROZEN_PREFIXES)
                   or p.name in ('workflow_manifest.json', 'RESULT_SCHEMA.md'))}
    source = Path(manifest['source'])
    trace = Path(manifest['trace']) if manifest.get('trace') else None
    contract = {'schema': SCHEMA, 'orchestration_version': VERSION,
                'source': str(source), 'source_sha256': digest(source),
                'trace_sha256': digest(trace) if trace and trace.is_file() else None,
                'frozen_artifacts': frozen, 'batches': batches,
                'manual_contents_read': False}
    write_json(run / CONTRACT, contract)
    return contract


def csv_rows(path):
    if not path.is_file() or not path.stat().st_size:
        return []
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def description_check(run, plan, errors):
    """Check accounting/references, leaving lexical and semantic rules to v9.25."""
    expected_empty = {str(r['行号']) for r in csv_rows(run / 'action_desc_a.csv')
                      if not str(r.get('A档描述') or '').strip()}
    all_inputs = set()
    for batch in (b for b in plan if b['kind'] == 'description'):
        payload = read_json(local_file(run, batch['input']))
        rows = {str(r['id']): r for r in payload.get('rows', [])}
        if len(rows) != len(payload.get('rows', [])) or all_inputs.intersection(rows):
            errors.append('B档输入行号重复: ' + batch['input'])
        all_inputs.update(rows)
        result_path = local_file(run, batch['output'])
        if not result_path.is_file():
            continue
        result = read_json(result_path)
        descs = result.get('descs')
        if not isinstance(descs, dict) or set(descs) != set(rows):
            errors.append('B档结果必须逐行处理本批输入，不能漏行或越批: ' + batch['output'])
            continue
        for rid, entry in descs.items():
            if not isinstance(entry, dict) or not isinstance(entry.get('ok'), bool):
                errors.append('B档记录缺少布尔 ok: ' + rid)
                continue
            if not entry['ok']:
                if not str(entry.get('reason') or '').strip():
                    errors.append('B档无法描述时必须给出原因: ' + rid)
                continue
            tokens = {str(t['tok']) for t in rows[rid].get('tokens', [])}
            refs = entry.get('refs', [])
            if not isinstance(refs, list) or any(not isinstance(x, str) or x not in tokens for x in refs):
                errors.append('B档引用超出本行动作变量: ' + rid)
            if not isinstance(entry.get('desc'), str) or not entry['desc'].strip():
                errors.append('B档可用记录缺少描述: ' + rid)
    if (run / 'action_desc_a.csv').is_file() and all_inputs != expected_empty:
        errors.append('B档输入范围与A档待补充行不一致，需在新运行目录重新准备')


def delivery_check(run, manifest):
    errors = []
    status = read_json(run / 'workflow_status.json')
    if not status.get('success'):
        errors.append('工艺批次组装未成功')
    if status.get('action_desc', {}).get('error'):
        errors.append('动作描述合并未完成: ' + status['action_desc']['error'])
    required = [
        'final_prediction_pairs.csv', 'final_prediction_pairs_full.csv',
        'final_sequence.csv', 'final_sequence_raw.csv',
        'final_process_steps.csv', 'final_process_steps_raw.csv',
        'unresolved_events.csv', 'process_contract_check.csv', 'process_review_queue.csv',
        'action_desc_final.csv', 'workflow_status.json', 'review.md',
    ]
    required += [f'{prefix}_atomic_{suffix}' for prefix in ('final', 'process', 'sequence')
                 for suffix in ('lineage.csv', 'conditions.json', 'review.csv')]
    if (run / 'action_desc_review.csv').is_file():
        required.append('action_desc_review.csv')
    for name in required:
        if not (run / name).is_file():
            errors.append('交付物缺失: ' + name)
    for key, requested in (('workstation_xlsx', True), ('trace_analysis', bool(manifest.get('trace'))),
                           ('coverage', bool(manifest.get('manual')))):
        if not requested:
            continue
        detail = status.get(key)
        if not isinstance(detail, dict) or detail.get('error'):
            errors.append('请求的交付能力未完成: ' + key)
            continue
        if key == 'trace_analysis' and not detail.get('outputs'):
            errors.append('Trace分析缺少输出清单')
        files = detail.get('outputs', {}).values() if key == 'trace_analysis' else [detail.get('out')]
        for value in files:
            if not value or not Path(value).is_file():
                errors.append('交付文件不存在: ' + str(value))
            else:
                required.append(str(Path(value).resolve()))
    descriptions = csv_rows(run / 'action_desc_final.csv')
    review = {
        'boundary_rows': len(csv_rows(run / 'final_atomic_review.csv')),
        'unresolved_events': len(csv_rows(run / 'unresolved_events.csv')),
        'process_rows': len(csv_rows(run / 'process_review_queue.csv')),
        'description_rows': len(csv_rows(run / 'action_desc_review.csv')),
        'empty_descriptions': sum(not str(r.get('动作描述', r.get('A档描述', '')) or '').strip()
                                  for r in descriptions),
    }
    artifacts = {}
    for name in required:
        path = Path(name) if Path(name).is_absolute() else run / name
        if path.is_file():
            artifacts[str(path.relative_to(run)) if run in path.parents else str(path)] = digest(path)
    return errors, review, artifacts


def audit_run(out, all_batches=False):
    """Read-only progress, integrity and next work. Never reads manual contents."""
    run = Path(out).resolve()
    result = {'orchestration_version': VERSION, 'run': str(run), 'state': 'INVALID_INPUTS',
              'ready_to_finish': False, 'completed': False, 'errors': [], 'warnings': []}
    errors = result['errors']
    try:
        manifest = read_json(run / 'workflow_manifest.json')
        contract = read_json(run / CONTRACT) if (run / CONTRACT).exists() else None
        if contract is not None and contract.get('schema') != SCHEMA:
            raise ValueError('执行清单版本不支持')
        result['input_binding'] = 'sha256' if contract else 'legacy_unsealed'
        if not contract:
            result['warnings'].append('旧运行无输入指纹；可兼容核验，不能证明旧结果与原输入字节绑定')
        plan = batch_plan(run, manifest)
        if contract:
            expected = contract['batches']
            if [{k: b[k] for k in ('kind', 'batch_id', 'input', 'output')} for b in expected] != plan:
                errors.append('批次清单变化；不能混用旧结果')
            expected_map = {b['input']: b['input_sha256'] for b in expected}
            for name, sha in contract['frozen_artifacts'].items():
                if not local_file(run, name).is_file() or digest(local_file(run, name)) != sha:
                    errors.append('准备阶段证据已变化: ' + name)
            source = Path(manifest.get('source', ''))
            if not source.is_file() or digest(source) != contract['source_sha256']:
                errors.append('源码文件与本次准备阶段不一致')
            if contract.get('trace_sha256'):
                trace = Path(manifest.get('trace', ''))
                if not trace.is_file() or digest(trace) != contract['trace_sha256']:
                    errors.append('Trace文件与本次准备阶段不一致')
        else:
            expected_map = {}
        for key in ('trace', 'manual'):
            if manifest.get(key) and not Path(manifest[key]).is_file():
                errors.append('请求的输入文件不存在: ' + key)
        if not (run / 'action_desc_a.csv').is_file():
            errors.append('动作描述准备未完成: action_desc_a.csv')
        pending = []
        completed = {'process': 0, 'description': 0}
        totals = {'process': 0, 'description': 0}
        hashes = {}
        for batch in plan:
            totals[batch['kind']] += 1
            source = local_file(run, batch['input'])
            actual_sha = digest(source)
            sha = expected_map.get(batch['input'], actual_sha)
            if actual_sha != sha:
                errors.append('批次输入已变化: ' + batch['input'])
            output = local_file(run, batch['output'])
            order = {**batch, 'input_sha256': sha}
            if not output.is_file():
                pending.append(order)
                continue
            data = read_json(output)
            if not isinstance(data, dict):
                raise ValueError('批次结果必须为JSON对象: ' + batch['output'])
            if data.get('batch_id') != batch['batch_id'] and (contract or 'batch_id' in data or batch['kind'] == 'process'):
                errors.append('结果批次ID不一致: ' + batch['output'])
            if contract and data.get('input_sha256') != sha:
                errors.append('结果未绑定当前输入；不得给旧结果批量补指纹: ' + batch['output'])
            if batch['kind'] == 'process' and (not isinstance(data.get('steps'), list)
                                               or not isinstance(data.get('unresolved_events'), list)):
                errors.append('工艺结果缺少steps/unresolved_events数组: ' + batch['output'])
            completed[batch['kind']] += 1
            hashes[batch['output']] = digest(output)
        expected_outputs = {b['output'] for b in plan}
        for folder in ('batches', 'batches_desc'):
            for path in (run / folder).glob('*.result.json'):
                if path.relative_to(run).as_posix() not in expected_outputs:
                    errors.append('存在不属于当前清单的旧结果: ' + path.name)
        description_check(run, plan, errors)
        from workflow import validate
        legacy = validate(run, write=False, source_fallback=False)
        errors.extend(legacy['invalid_issues'])
        if errors:
            result['state'] = 'INVALID_RESULTS'
        elif pending:
            result['state'] = 'WAITING_FOR_BATCHES'
        else:
            result['state'] = 'READY_TO_FINISH'
            result['ready_to_finish'] = True
        result['batches'] = {kind: {'total': totals[kind], 'completed': completed[kind],
                                    'pending': totals[kind] - completed[kind]} for kind in totals}
        result['result_sha256'] = hashes
        result['required_skills'] = BASE_SKILLS + (['plc-trace-jump-analysis'] if manifest.get('trace') else []) + (['plc-coverage-evaluation'] if manifest.get('manual') else [])
        if all_batches:
            result['pending_work'] = pending
        else:
            result['next_work'] = [next(b for b in pending if b['kind'] == kind)
                                   for kind in totals if any(b['kind'] == kind for b in pending)]
        if result['ready_to_finish'] and (run / COMPLETION).is_file():
            receipt = read_json(run / COMPLETION)
            same = (receipt.get('schema') == 'plc.workflow_completion.v1' and bool(receipt.get('artifacts'))
                    and receipt.get('result_sha256') == hashes
                    and receipt.get('contract_sha256') == (digest(run / CONTRACT) if contract else None))
            same = same and all(Path(p if Path(p).is_absolute() else run / p).is_file()
                                and digest(p if Path(p).is_absolute() else run / p) == sha
                                for p, sha in receipt.get('artifacts', {}).items())
            if same and receipt.get('completed'):
                result.update(state=receipt['state'], completed=True, ready_to_finish=False,
                              review=receipt.get('review', {}))
            elif not receipt.get('completed'):
                result.update(state='DELIVERY_INCOMPLETE', delivery_errors=receipt.get('errors', []),
                              review=receipt.get('review', {}))
            else:
                result['warnings'].append('结果或交付物已变化，需要重新complete；不会由status改写文件')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        errors.append(str(exc))
    return result


def complete_run(out):
    """Finish only a fully accounted run; preserve the legacy engine outputs."""
    before = audit_run(out)
    if before['completed'] or not before['ready_to_finish']:
        return before
    run = Path(out).resolve()
    from workflow import validate
    core = validate(run, write=True)
    manifest = read_json(run / 'workflow_manifest.json')
    try:
        errors, review, artifacts = delivery_check(run, manifest)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors, review, artifacts = [str(exc)], {}, {}
    if not core.get('success'):
        errors.append('v9.25组装校验未通过')
    after = audit_run(run)
    errors.extend(after['errors'])
    if after.get('result_sha256') != before['result_sha256']:
        errors.append('批次结果在组装期间变化，不能确认本次交付完成')
    state = 'DELIVERY_INCOMPLETE' if errors else 'COMPLETED_WITH_REVIEW' if any(review.values()) else 'COMPLETED'
    receipt = {'schema': 'plc.workflow_completion.v1', 'state': state, 'completed': not errors,
               'contract_sha256': digest(run / CONTRACT) if (run / CONTRACT).is_file() else None,
               'result_sha256': before['result_sha256'], 'review': review,
               'artifacts': artifacts, 'errors': errors, 'orchestration_version': VERSION}
    write_json(run / COMPLETION, receipt)
    return {**before, **receipt, 'ready_to_finish': False, 'core_success': bool(core.get('success'))}
