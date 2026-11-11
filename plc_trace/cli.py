#!/usr/bin/env python3
"""Trace analysis and capture setup using the full workflow source pipeline."""
import argparse, json, sys
from pathlib import Path
from plc_trace import __version__
from plc_trace.runtime.workflow import create, fingerprint, lock_run_dir, pipeline_fingerprint

def _extract_source_evidence(src,out,include_comments=True,include_station_identity=True):
  """Extract variable_docs + station_identity (+ step_comments + network_comments
  when include_comments=True).  Each step is non-fatal — failure only degrades
  display names or descriptions, never blocks the main extraction.
  Called by the same Trace preparation path as full-workflow."""
  try:
   from plc_trace.source.extract_variable_docs import extract as extract_vdocs
   extract_vdocs(str(src),str(out/'variable_documentation.json'))
  except Exception as exc:
   print(f'variable_documentation 提取失败（不影响主流程）：{exc}',file=sys.stderr)
  if include_comments:
   try:
    from plc_trace.source.extract_network_comments import extract as extract_ncomments
    extract_ncomments(str(src),str(out/'network_comments.json'))
   except Exception as exc:
    print(f'network_comments 提取失败（不影响主流程）：{exc}',file=sys.stderr)
   try:
    from plc_trace.source.extract_step_comments import extract as extract_scomments
    extract_scomments(str(src),str(out/'step_comments.json'))
   except Exception as exc:
    print(f'step_comments 提取失败（不影响主流程）：{exc}',file=sys.stderr)
  if include_station_identity:
   try:
    from plc_trace.output.station_identity import build_identity
    si_map,_=build_identity(str(out))
    (out/'station_identity.json').write_text(json.dumps(si_map,ensure_ascii=False,indent=1),encoding='utf-8')
   except Exception as exc:
    print(f'station_identity 构建失败（不影响主流程）：{exc}',file=sys.stderr)


_TRACE_PREPARED = 'trace_prepared.json'
_TRACE_STATS = ('variables_total', 'variables_active', 'total_jumps', 'cycles_detected',
                'unique_pairs', 'pairs_with_action_vars', 'pairs_with_step_comment',
                'pairs_reset_exit', 'review_count', 'source_relations', 'source_evidence_audit',
                'proc_name', 'trace_input')


def _trace_inputs(src, trace, args):
 from plc_trace.trace.trace_to_pairs import trace_input_digest
 from plc_trace.description.trace_description_context import SCHEMA
 return {
     'source': str(src), 'source_sha256': fingerprint(src),
     'trace': str(trace), 'trace_sha256': trace_input_digest(trace),
     'description_schema': SCHEMA, 'pipeline_sha256': pipeline_fingerprint(),
     'options': {name: getattr(args, name) for name in ('batch_size', 'pattern', 'proc_name', 'sample_interval_ms')},
 }


def _trace_prepared_files(out):
 files = sorted(p for suffix in ('csv', 'json') for p in out.glob(f'trace_*.{suffix}')
                if p.name != _TRACE_PREPARED)
 files += [out/'action_desc_a.csv', out/'workflow_manifest.json']
 for name in ('variable_documentation.json', 'step_comments.json', 'station_identity.json',
              'native_command_evidence.csv', 'plcopen_action_trace.csv', 'source_stage_candidates.csv',
              'source_transition_edges.csv', 'indexed_candidates.csv', 'indexed_events.csv'):
  if (out/name).is_file():
   files.append(out/name)
 files += sorted((out/'batches_desc').glob('*.input.json'))
 return {p.relative_to(out).as_posix(): fingerprint(p) for p in files}


def _run_trace_mode(src,out,args):
 """Prepare observed Trace pairs, then validate descriptions and render."""
 trace = Path(args.trace).resolve()
 if not src.is_file():
  raise FileNotFoundError(f'PLC源码不存在：{src}')
 if not trace.exists():
  raise FileNotFoundError(f'Trace输入不存在：{trace}')
 if any(p.is_relative_to(out) for p in (src,trace,Path(args.source).absolute(),Path(args.trace).absolute())):
  raise ValueError('输出目录须与PLC源码和Trace输入分开')
 cache_path = out/_TRACE_PREPARED
 inputs = _trace_inputs(src, trace, args)
 cached = None
 if args.resume:
  try:
   cached = json.loads(cache_path.read_text(encoding='utf-8'))
   manifest = json.loads((out/'workflow_manifest.json').read_text(encoding='utf-8'))
   if (manifest.get('mode') != 'trace' or not cached['run_id'] or cached['run_id'] != manifest.get('run_id')
       or cached['inputs'] != inputs or cached['files'] != _trace_prepared_files(out)
       or not set((*_TRACE_STATS, 'candidate_count')) <= cached['stats'].keys()
       or (out/'final_prediction_pairs.csv').exists()):
    raise ValueError('输入或准备产物已变化')
  except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
   raise SystemExit(f'Trace准备结果无法续跑（{exc}）。请用新目录或显式 --force 重建。') from exc
 stats = {'version':__version__,'mode':'trace','source':str(src),'trace':str(trace),'out':str(out),
          'preparation_reused': cached is not None}
 stage = 'source'
 try:
  if cached is None:
   print('Trace [1/4] 解析 PLC 源码并准备候选批次', file=sys.stderr, flush=True)
   manifest = create(str(src),str(out),batch_size=args.batch_size,
                     pattern=args.pattern,force=args.force,trace=trace,mode='trace')
   stats.update(run_id=manifest['run_id'],previous_output=manifest['previous_output'],
                candidate_count=manifest.get('candidate_count',0))

   stage = 'source_evidence'
   print('Trace [2/4] 提取源码变量文档和工位身份', file=sys.stderr, flush=True)
   _extract_source_evidence(src,out,include_comments=manifest['extraction']['format'] != 'plcopen')

   stage = 'trace'
   print('Trace [3/4] 解析采集数据并汇总状态对', file=sys.stderr, flush=True)
   from plc_trace.trace.trace_to_pairs import generate as gen_trace_pairs
   trace_stats = gen_trace_pairs(str(trace),str(out),source=str(src),pattern=args.pattern,
                                 proc_name=args.proc_name,
                                 sample_interval_ms=args.sample_interval_ms)
   stats.update({k: trace_stats[k] for k in _TRACE_STATS})
   stage = 'description'
   print('Trace [4/4] 准备动作描述批次', file=sys.stderr, flush=True)
   from plc_trace.description.action_describe import cmd_a, cmd_build
   cmd_a(str(out))
   cmd_build(str(out))
   if _trace_inputs(src, trace, args) != inputs:
    raise ValueError('准备过程中源码、Trace或程序发生变化，请重建')
   prepared = {'run_id': stats['run_id'], 'inputs': inputs, 'files': _trace_prepared_files(out),
               'stats': {k: stats[k] for k in (*_TRACE_STATS, 'candidate_count')}}
   tmp_path = cache_path.with_suffix('.tmp')
   tmp_path.write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding='utf-8')
   tmp_path.replace(cache_path)
  else:
   print('Trace 续跑：准备产物已核验，合并 B 档结果并刷新交付表', file=sys.stderr, flush=True)
   stats.update(cached['stats'],run_id=cached['run_id'])

  stage = 'description'
  from plc_trace.description.action_describe import cmd_validate
  cmd_validate(str(out), stats=stats)

  stage = 'workbook'
  from plc_trace.output.gen_workstation_xlsx import generate as gen_ws
  proc = stats['proc_name'] or '工序'
  xlsx_out = str(out/f'{proc}_trace步序采集表.xlsx')
  stats['workstation_xlsx'] = gen_ws(str(out/'trace_collection_pairs.csv'),out=xlsx_out,
                                    proc_name=proc,run=str(out),sheet='trace步序采集表')
  if stats['b_tier_pending_rows']:
   stats['status'] = 'prepared_b_tier_pending'
  elif stats['review_count'] or stats['description_review_count']:
   stats['status'] = 'done_with_review'
  else:
   stats['status'] = 'done'
 except Exception as exc:
  stats.update(status='xlsx_failed' if stage == 'workbook' else 'failed',
               failed_stage=stage, error=str(exc))
 status = json.dumps(stats,ensure_ascii=False,indent=2)
 if stats.get('run_id'):
  status_path = out/'workflow_status.json'
  tmp_path = status_path.with_suffix('.tmp')
  tmp_path.write_text(status,encoding='utf-8')
  tmp_path.replace(status_path)
 print(status)
 if 'error' in stats:
  sys.exit(2)


def _expand_trace_shorthand(parser, args):
    """Translate the two Trace conversation forms to existing subcommands."""
    if not args or args[0].startswith('-') or Path(args[0]).suffix.lower() not in ('.xml', '.smc2', '.txt'):
        return args

    routes = {'-t': ('trace', '--trace'), '--trace-setup': ('trace-setup', '--pattern')}
    if len(args) < 3 or args[2] not in routes or not args[1].strip() or args[1].startswith('-'):
        parser.error('Trace 简写须为 源码 Trace路径 -t 或 源码 匹配模式 --trace-setup')

    source, value, marker, *suffix = args
    root = Path('outputs')
    if suffix:
        if len(suffix) != 2 or suffix[0] != '-output' or not suffix[1].strip() or suffix[1].startswith('-'):
            parser.error('简写的输出选项须在末尾写成 -output "输出根目录"')
        root = Path(suffix[1])

    from plc_trace.output.utils import infer_proc_name
    proc_name = infer_proc_name(source)
    name = proc_name + ('_trace_setup' if marker == '--trace-setup' else '_trace')
    out = root / name
    index = 2
    while out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
        out = root / f'{name}_{index}'
        index += 1

    mode, option = routes[marker]
    return [mode, f'--source={source}', f'{option}={value}', f'--out={out}']


def main():
 p=argparse.ArgumentParser(description='PLC Trace 步序采集与采集配置')
 p.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
 p.epilog = ('Trace 简写可用 源码 Trace路径 -t [-output 输出根目录] '
             '或 源码 匹配模式 --trace-setup [-output 输出根目录]')
 sub=p.add_subparsers(dest='mode',required=True)
 x=sub.add_parser('trace',help='trace步序采集模式：trace跳变→状态对→A/B档动作描述→交付表')
 x.add_argument('--source',required=True,help='PLC源码XML路径（用于动作变量映射/步注释/工位身份）')
 x.add_argument('--trace',required=True,help='trace输入：TwinCAT .trace文件 / Sysmac CSV表 / Sysmac采集目录')
 x.add_argument('--sample-interval-ms',type=float,help='Sysmac 单次采集的 RawTime 精度不足时指定采样间隔(ms)')
 x.add_argument('--proc-name',default=None,help='工序名称；缺省优先使用源码所在工序目录名')
 x.add_argument('--out',required=True,help='输出目录')
 x.add_argument('--pattern',default=None,help='可选：变量名称过滤模式（glob或re:前缀正则）')
 x.add_argument('--batch-size',type=int,default=120)
 execution=x.add_mutually_exclusive_group()
 execution.add_argument('--force',action='store_true',help='归档旧输出目录并从空目录开始')
 execution.add_argument('--resume',action='store_true',help='校验并合并同一轮的B档批次')
 x.set_defaults(func=lambda a:_run_trace_mode(Path(a.source).resolve(),Path(a.out).resolve(),a))
 from plc_trace.capture.extract_step_variables import add_arguments as setup_arguments, run as trace_setup
 x=sub.add_parser('trace-setup',help='从源码提取变量并生成 Trace 采集配置')
 setup_arguments(x)
 x.set_defaults(func=trace_setup)
 a=p.parse_args(_expand_trace_shorthand(p, sys.argv[1:]))
 if a.mode=='trace':
  with lock_run_dir(a.out):
   a.func(a)
 else:
  a.func(a)
if __name__=='__main__':main()
