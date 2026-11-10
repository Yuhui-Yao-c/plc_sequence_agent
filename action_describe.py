"""Prepare, validate, and merge Trace A/B descriptions."""
import json
from pathlib import Path

import pandas as pd

from trace_description_context import (
    LEGACY_CLAUSE_SCHEMA, SCHEMA as TRACE_CONTEXT_SCHEMA, validate_context_result,
)
from trace_description_report import action_evidence_text, annotation_supplement
from trace_source_evidence import RELATION_TEXT, read_trace_evidence


_A_FIELDS = ['行号', '开始标签', '结束标签', '动作变量', 'A档描述', '行性质']


def cmd_a(run):
    run = Path(run)
    trace_evidence = read_trace_evidence(run)
    df = pd.read_csv(run / 'trace_collection_pairs.csv', dtype=str, encoding='utf-8-sig', keep_default_na=False)
    p = run / 'action_desc_a.csv'
    rows = []
    for i, r in df.iterrows():
        key = f"{r['开始标签']}|||{r['结束标签']}"
        evidence = trace_evidence.get(key, {})
        desc = evidence.get('description', '')
        rows.append({
            '行号': i, '开始标签': r['开始标签'], '结束标签': r['结束标签'],
            '动作变量': r.get('动作变量', ''), 'A档描述': desc,
            '行性质': '源码步序' if desc else '待核验',
        })
    pd.DataFrame(rows, columns=_A_FIELDS).to_csv(p, index=False, encoding='utf-8-sig')
    print(f"Trace A档: {sum(bool(r['A档描述']) for r in rows)}/{len(rows)}行有对齐源码注释")
    return p



def _description_run_id(run):
    manifest = Path(run) / 'workflow_manifest.json'
    if manifest.is_file():
        return json.loads(manifest.read_text(encoding='utf-8')).get('run_id')
    return None


def cmd_build(run, batch_size=40):
    run = Path(run)
    df = pd.read_csv(run / 'action_desc_a.csv', dtype=str, encoding='utf-8-sig', keep_default_na=False)
    df = df[df['A档描述'].str.strip() == ''].reset_index(drop=True)
    outdir = run / 'batches_desc'
    outdir.mkdir(exist_ok=True)
    return _build_trace_batches(df, outdir, read_trace_evidence(run), batch_size, _description_run_id(run))


def _build_trace_batches(df, outdir, evidence, batch_size, run_id):
    if batch_size < 1:
        raise ValueError('B档batch_size须大于0')
    rows = []
    for _, row in df.iterrows():
        key = f"{row['开始标签']}|||{row['结束标签']}"
        info = evidence.get(key, {})
        context = info.get('description_context', {})
        if context.get('schema') != TRACE_CONTEXT_SCHEMA:
            raise ValueError('Trace描述上下文需更新，请用当前版本运行trace --force以重建证据包')
        rows.append({
            'id': key, 'basis': info['basis'], 'start': row['开始标签'], 'end': row['结束标签'],
            'context': context,
        })
    chunks, chunk, size = [], [], 0
    for row in rows:
        row_size = len(json.dumps(row, ensure_ascii=False))
        if chunk and (len(chunk) >= batch_size or size + row_size > 80_000):
            chunks.append(chunk); chunk, size = [], 0
        chunk.append(row); size += row_size
    if chunk:
        chunks.append(chunk)
    for path in outdir.glob('*.input.json'):
        path.unlink()
    count = 0
    for count, chunk in enumerate(chunks, 1):
        payload = {
            'batch_id': count, 'schema': TRACE_CONTEXT_SCHEMA,
            'rows': chunk,
            'task': (
                '为起始步写中文动作分句actions。每个分句说明具体机构或处理对象及动作，保留有证据的目标位和多个动作对象。'
                '逐项核验context.required_actions，为每个锚点提交{action_ref: ID}。锚点包含对象、指令、目标及原始证据引用。'
                '其他动作提交{subject, action, source_ref, subject_symbol, evidence_refs}。'
                'source_ref指向本步指令效果、控制条件或注释。证据有subjects时，从同一项选择完整symbol作为subject_symbol。'
                '引用step_condition时填写condition_use为wait或check，action填写具体条件，程序会加上等待或判断。'
                '所选对象带numeric_predicate时只填subject、source_ref、subject_symbol和condition_use，省略action和target，由程序渲染源码数值判据。'
                'source_write写入保留对象、值和门控。定位命令、结果写入和反馈判断分别描述，避免把条件说成执行动作。'
                '如引用本步注释且其中明确包含对象和动作，可省略subject_symbol。'
                '有目标时填写target，引用实参时用target_parameter写出同一调用中的形参名。'
                '工艺词和kind按当前工程表达。命名类比只用于识别对象，不能当动作依据。'
                '步号不匹配的注释仅作上下文。区分指令、反馈与完成，区分超时监控和正常等待。'
                '保留basis、kind及reasoning。缺证据时提交ok=false和明确的reason。交付描述由通过的分句合成。'
            ),
        }
        if run_id:
            payload['run_id'] = run_id
            payload['task'] += '结果原样回传本批次 run_id 和 batch_id。'
        path = outdir / f'batch_{count:03d}.input.json'
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'Trace B档: 全部{len(rows)}行缺注释状态对进入上下文描述，{count}批')
    return count


def _description_results(run, run_id):
    results, missing, invalid = {}, [], []
    inputs = sorted((run / 'batches_desc').glob('*.input.json'))
    for path in inputs:
        batch = json.loads(path.read_text(encoding='utf-8-sig'))
        result_path = path.with_name(path.name.replace('.input.json', '.result.json'))
        if not result_path.is_file():
            missing.append(result_path.name)
            continue
        try:
            result = json.loads(result_path.read_text(encoding='utf-8-sig'))
            if (result['batch_id'] != batch['batch_id'] or not isinstance(result['descs'], dict)
                    or (run_id and (batch.get('run_id') != run_id or result.get('run_id') != run_id))):
                raise ValueError('batch or run mismatch')
        except (OSError, ValueError, KeyError, TypeError):
            invalid.append(result_path.name)
            continue
        for row in batch['rows']:
            key = row['id'] if row['id'] in result['descs'] else row.get('row_id')
            if key in result['descs']:
                results[row['id']] = result['descs'][key]
    return results, {
        'b_tier_input_count': len(inputs),
        'b_tier_result_count': len(inputs) - len(missing) - len(invalid),
        'missing_b_tier_batches': missing,
        'invalid_b_tier_batches': invalid,
    }


def _validate_trace(run, a, evidence, stats):
    run = Path(run)
    results, counts = _description_results(run, _description_run_id(run))
    counts.update(a_sourced=0, b_candidates=0, unresolved_rows=0, b_tier_pending_rows=0,
                  b_tier_invalid_rows=0)
    rows, review = [], []
    for row in a.to_dict('records'):
        candidate = {}
        key = f"{row['开始标签']}|||{row['结束标签']}"
        info = evidence.get(key, {})
        desc, kind, refs = info.get('description', ''), row['行性质'], info.get('description_refs', [])
        source = info.get('description_source', '') or '待核验'
        status, reason = '证据待核验', ''
        if desc:
            counts['a_sourced'] += 1
            status = '源码注释'
        elif context := info.get('description_context'):
            if key not in results:
                status, reason = 'B档待处理', '缺少本行B档结果'
                counts['b_tier_pending_rows'] += 1
            else:
                candidate, reason = validate_context_result(results[key], context)
                if candidate:
                    desc, kind, refs = candidate['desc'], candidate['kind'], candidate['refs']
                    source, status = 'B档(上下文推断)', 'LLM推断待核验'
                    reason = 'B档引用通过，工艺语义待核验'
                    counts['b_candidates'] += 1
                elif (context.get('schema') in (TRACE_CONTEXT_SCHEMA, LEGACY_CLAUSE_SCHEMA)
                      and not (isinstance(results[key], dict) and results[key].get('basis') == info['basis']
                               and results[key].get('ok') is False and isinstance(results[key].get('reason'), str)
                               and results[key]['reason'].strip())):
                    status = 'B档待修订'
                    counts['b_tier_invalid_rows'] += 1
                    counts['b_tier_pending_rows'] += 1
                else:
                    counts['unresolved_rows'] += 1
        else:
            relation = info.get('relation')
            reason = ('缺少可用于描述的本网络证据' if relation == 'direct'
                      else RELATION_TEXT.get(relation, '缺少Trace源码证据，需重跑'))
            counts['unresolved_rows'] += 1
        record = {k: row[k] for k in ('行号', '开始标签', '结束标签', '动作变量')}
        rows.append({
            **record, '动作描述': desc, '行性质': kind, '描述来源': source,
            '描述状态': status, '证据引用': '; '.join(refs),
            '推断依据': candidate.get('reasoning', '') if status == 'LLM推断待核验' else '',
            '证据等级': candidate.get('evidence_level', '') if status == 'LLM推断待核验' else '源码注释' if desc else '',
            '注释补充': annotation_supplement(info) if status == '源码注释' else '',
            '证据': action_evidence_text(info, candidate) if candidate else '',
        })
        if reason:
            review.append({**record, '描述状态': status, '原因': reason})
    fields = ['行号', '开始标签', '结束标签', '动作变量']
    out = run / 'action_desc_final.csv'
    columns = fields + ['动作描述', '行性质', '描述来源', '描述状态', '证据引用', '推断依据', '证据等级', '注释补充', '证据']
    pd.DataFrame(rows, columns=columns).to_csv(out, index=False, encoding='utf-8-sig')
    pd.DataFrame(review, columns=fields + ['描述状态', '原因']).to_csv(
        run / 'action_desc_review.csv', index=False, encoding='utf-8-sig')
    counts['description_review_count'] = len(review)
    counts['description_total'] = len(rows)
    counts['description_filled'] = counts['a_sourced'] + counts['b_candidates']
    if stats is not None:
        stats.update(counts)
    print(f'Trace动作描述: 源码注释{counts["a_sourced"]}行，B档候选{counts["b_candidates"]}行，'
          f'证据待核{counts["unresolved_rows"]}行，B档待处理{counts["b_tier_pending_rows"]}行')
    return str(out)


def cmd_validate(run, *, stats=None):
    run = Path(run)
    evidence = read_trace_evidence(run)
    a = pd.read_csv(run / 'action_desc_a.csv', dtype=str, encoding='utf-8-sig', keep_default_na=False)
    return _validate_trace(run, a, evidence, stats)
