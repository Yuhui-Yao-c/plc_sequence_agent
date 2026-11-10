#!/usr/bin/env python3
"""Prepare, resume, and export an observed PLC Trace collection."""
import argparse
import json
import sys
from pathlib import Path

from workflow import create, fingerprint, lock_run_dir, pipeline_fingerprint


_TRACE_PREPARED = 'trace_prepared.json'
_TRACE_STATS = (
    'variables_total', 'variables_active', 'total_jumps', 'cycles_detected',
    'unique_pairs', 'pairs_with_action_vars', 'pairs_with_step_comment',
    'pairs_reset_exit', 'review_count', 'source_relations', 'source_evidence_audit',
    'proc_name', 'trace_input',
)


def _prepare_documents(source, out, source_format):
    from extract_variable_docs import extract as variable_docs
    from extract_step_comments import extract as step_comments

    extracts = [('variable_documentation.json', variable_docs)]
    if source_format != 'plcopen':
        extracts.append(('step_comments.json', step_comments))
    for filename, extract in extracts:
        try:
            extract(str(source), str(out / filename))
        except Exception as exc:
            print(f'{filename} 提取失败，保留源码控制证据：{exc}', file=sys.stderr)


def _trace_inputs(source, trace, args):
    from trace_description_context import SCHEMA
    from trace_to_pairs import trace_input_digest

    return {
        'source': str(source), 'source_sha256': fingerprint(source),
        'trace': str(trace), 'trace_sha256': trace_input_digest(trace),
        'description_schema': SCHEMA, 'pipeline_sha256': pipeline_fingerprint(),
        'options': {name: getattr(args, name) for name in (
            'batch_size', 'pattern', 'proc_name', 'sample_interval_ms')},
    }


def _trace_prepared_files(out):
    files = sorted(p for suffix in ('csv', 'json') for p in out.glob(f'trace_*.{suffix}')
                   if p.name != _TRACE_PREPARED)
    files += [out / 'action_desc_a.csv', out / 'workflow_manifest.json']
    for name in ('variable_documentation.json', 'step_comments.json', 'station_identity.json'):
        if (out / name).is_file():
            files.append(out / name)
    files += sorted((out / 'batches_desc').glob('*.input.json'))
    return {p.relative_to(out).as_posix(): fingerprint(p) for p in files}


def _write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def _resume(out, inputs):
    try:
        cached = json.loads((out / _TRACE_PREPARED).read_text(encoding='utf-8'))
        manifest = json.loads((out / 'workflow_manifest.json').read_text(encoding='utf-8'))
        if (manifest.get('mode') != 'trace' or not cached['run_id']
                or cached['run_id'] != manifest.get('run_id') or cached['inputs'] != inputs
                or cached['files'] != _trace_prepared_files(out)
                or not set((*_TRACE_STATS, 'source_format')) <= cached['stats'].keys()):
            raise ValueError('输入或准备产物已变化')
        return cached
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SystemExit(f'Trace准备结果无法续跑（{exc}）。请用新目录或显式 --force 重建。') from exc


def _run_trace(args):
    from action_describe import cmd_a, cmd_build, cmd_validate
    from gen_workstation_xlsx import generate as render_workbook
    from trace_to_pairs import generate as generate_pairs

    source, trace, out = (Path(path).resolve() for path in (args.source, args.trace, args.out))
    if not source.is_file():
        raise FileNotFoundError(f'PLC源码不存在：{source}')
    if not trace.exists():
        raise FileNotFoundError(f'Trace输入不存在：{trace}')
    if any(p.is_relative_to(out) for p in (source, trace, Path(args.source).absolute(), Path(args.trace).absolute())):
        raise ValueError('输出目录须与PLC源码和Trace输入分开')
    inputs = _trace_inputs(source, trace, args)
    cached = _resume(out, inputs) if args.resume else None
    stats = {'mode': 'trace', 'source': str(source), 'trace': str(trace), 'out': str(out),
             'preparation_reused': cached is not None}
    stage = 'source'
    try:
        if cached is None:
            manifest = create(source, out, trace=trace, force=args.force,
                              source_sha256=inputs['source_sha256'], pipeline_sha256=inputs['pipeline_sha256'])
            source_format = manifest['extraction']['format']
            stats.update(run_id=manifest['run_id'], previous_output=manifest['previous_output'],
                         source_format=source_format)
            print('Trace [1/4] 读取源码变量文档', file=sys.stderr, flush=True)
            _prepare_documents(source, out, source_format)

            stage = 'trace'
            print('Trace [2/4] 汇总观测状态对并提取本步源码证据', file=sys.stderr, flush=True)
            trace_stats = generate_pairs(trace, out, source=source, pattern=args.pattern,
                                         proc_name=args.proc_name, sample_interval_ms=args.sample_interval_ms)
            stats.update({k: trace_stats[k] for k in _TRACE_STATS})

            stage = 'description'
            print('Trace [3/4] 准备动作描述批次', file=sys.stderr, flush=True)
            cmd_a(out)
            cmd_build(out, batch_size=args.batch_size)
            if _trace_inputs(source, trace, args) != inputs:
                raise ValueError('准备过程中源码、Trace或程序发生变化，请重建')
            _write_json(out / _TRACE_PREPARED, {
                'run_id': stats['run_id'], 'inputs': inputs, 'files': _trace_prepared_files(out),
                'stats': {k: stats[k] for k in (*_TRACE_STATS, 'source_format')},
            })
        else:
            print('Trace 续跑：准备产物已核验，合并本轮 B 档结果', file=sys.stderr, flush=True)
            stats.update(cached['stats'], run_id=cached['run_id'])

        stage = 'description'
        cmd_validate(out, stats=stats)
        stage = 'workbook'
        print('Trace [4/4] 导出步序工作簿', file=sys.stderr, flush=True)
        proc_name = stats['proc_name'] or '工序'
        stats['workstation_xlsx'] = render_workbook(
            str(out / 'trace_collection_pairs.csv'), out=str(out / f'{proc_name}_trace步序采集表.xlsx'),
            proc_name=proc_name, run=str(out),
        )
        if stats['b_tier_pending_rows']:
            stats['status'] = 'prepared_b_tier_pending'
        elif stats['review_count'] or stats['description_review_count']:
            stats['status'] = 'done_with_review'
        else:
            stats['status'] = 'done'
    except Exception as exc:
        stats.update(status='xlsx_failed' if stage == 'workbook' else 'failed',
                     failed_stage=stage, error=str(exc))
    if stats.get('run_id'):
        _write_json(out / 'workflow_status.json', stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if 'error' in stats:
        sys.exit(2)


def main():
    parser = argparse.ArgumentParser(description='PLC Trace 步序采集')
    commands = parser.add_subparsers(dest='mode', required=True)
    trace = commands.add_parser('trace', help='生成观测状态对、动作描述和步序工作簿')
    trace.add_argument('--source', required=True, help='PLC源码 XML、SMC2 或 XML 文本导出')
    trace.add_argument('--trace', required=True, help='TwinCAT .trace 文件或 Sysmac CSV、TSV、TXT 采集')
    trace.add_argument('--out', required=True, help='输出目录')
    trace.add_argument('--pattern', help='完整变量名、通配符或 re: 正则')
    trace.add_argument('--proc-name', help='工序名称，缺省优先使用源码所在工序目录名')
    trace.add_argument('--sample-interval-ms', type=float, help='Sysmac 时间精度不足时的已知采样间隔')
    trace.add_argument('--batch-size', type=int, default=40, help='B 档每批最大行数，默认 40')
    execution = trace.add_mutually_exclusive_group()
    execution.add_argument('--force', action='store_true', help='归档旧输出目录后重建')
    execution.add_argument('--resume', action='store_true', help='合并同一轮 B 档结果')
    args = parser.parse_args()
    if args.batch_size < 1:
        trace.error('--batch-size 须大于 0')
    with lock_run_dir(args.out):
        _run_trace(args)


if __name__ == '__main__':
    main()
