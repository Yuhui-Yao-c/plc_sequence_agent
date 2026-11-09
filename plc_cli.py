#!/usr/bin/env python3
"""PLC Agent v9.28 — four isolated user workflows.

  predict   : source XML → candidate pairs → batches → finish → delivery table
  evaluate  : prediction CSV + GT → match metrics (read-only, never modifies prediction)
  coverage  : prediction CSV + manual table → coverage evaluation
  trace     : trace file → state-jump pairs → A/B-tier descriptions → delivery table

Prediction and trace never open ground truth.  All four share the same
column-compatible state-pair CSV format so downstream action_describe /
gen_workstation_xlsx can be reused across modes.
"""
import argparse, json, sys, hashlib
from pathlib import Path
from workflow import create, validate
from pair_evaluation import run as evaluate_candidates

def fingerprint(p):
 h=hashlib.sha256()
 with Path(p).open('rb') as f:
  for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
 return h.hexdigest()

# Stale-product markers shared by -t and trace short-circuit modes.
_STALE_MARKERS=['workflow_manifest.json','workflow_status.json','batches',
                'batches_desc','final_prediction_pairs.csv']
# Stale file patterns to clear on --force (union of both modes' outputs).
_STALE_PATTERNS=('step_variables.json','step_variables.csv',
                 'trace_collection_pairs.csv','trace_*.csv','trace_*.json',
                 '*_步序变量表.xlsx','*_步序采集表.xlsx','*_trace步序采集表.xlsx',
                 'action_desc_*.csv','indexed_candidates.csv','indexed_events.csv',
                 'source_transition_graph.json','source_transition_edges.csv','source_stage_candidates.csv',
                 'native_command_evidence.csv','plcopen_action_trace.csv',
                 'prediction_*.csv','sequence_*.csv','final_*.csv',
                 'step_comments.json','network_comments.json',
                 'variable_documentation.json','station_identity.json')

def _check_isolation(out,args,mode_name):
  """Refuse to write into a dir with predict/finish products unless --force.
  Returns True if stale was cleared (caller should use force=True on create)."""
  markers = _STALE_MARKERS
  if mode_name == 'trace':
   markers = markers + ['trace_pairs_summary.json','source_transition_edges.csv',
                        'native_command_evidence.csv','plcopen_action_trace.csv']
  has_stale=any((out/m).exists() for m in markers)
  if has_stale and not args.force:
   raise SystemExit(
    f'{mode_name} 模式拒绝写入已有 predict/finish 产物的目录：{out}\n'
    f'  检测到残留：{[m for m in markers if (out/m).exists()]}\n'
    f'  请指定空目录，或加 --force 强制清理残留后重跑。')
  if has_stale and args.force:
   import shutil
   for m in markers:
    if m == 'batches_desc' and mode_name == 'trace':
     continue  # Rebuild inputs and validate retained results against the new source basis.
    p=out/m
    if p.is_dir(): shutil.rmtree(p,ignore_errors=True)
    elif p.exists(): p.unlink()
   for pat in _STALE_PATTERNS:
    for fp in out.glob(pat):
     try: fp.unlink()
     except Exception: pass
   return True
  return False

def _extract_source_evidence(src,out,include_comments=True,include_station_identity=True):
  """Extract variable_docs + station_identity (+ step_comments + network_comments
  when include_comments=True).  Each step is non-fatal — failure only degrades
  display names or descriptions, never blocks the main extraction.
  Shared by predict (include_station_identity=False, handles si_audit itself),
  _run_tagvars (include_comments=False), _run_trace_mode."""
  try:
   from extract_variable_docs import extract as extract_vdocs
   extract_vdocs(str(src),str(out/'variable_documentation.json'))
  except Exception as exc:
   print(f'variable_documentation 提取失败（不影响主流程）：{exc}',file=sys.stderr)
  if include_comments:
   try:
    from extract_network_comments import extract as extract_ncomments
    extract_ncomments(str(src),str(out/'network_comments.json'))
   except Exception as exc:
    print(f'network_comments 提取失败（不影响主流程）：{exc}',file=sys.stderr)
   try:
    from extract_step_comments import extract as extract_scomments
    extract_scomments(str(src),str(out/'step_comments.json'))
   except Exception as exc:
    print(f'step_comments 提取失败（不影响主流程）：{exc}',file=sys.stderr)
  if include_station_identity:
   try:
    from station_identity import build_identity
    si_map,_=build_identity(str(out))
    (out/'station_identity.json').write_text(json.dumps(si_map,ensure_ascii=False,indent=1),encoding='utf-8')
   except Exception as exc:
    print(f'station_identity 构建失败（不影响主流程）：{exc}',file=sys.stderr)

def _run_tagvars(src,out,args):
 """v9.26 --tagvars/-t branch: reuse predict's candidate pipeline to extract
 the step-variable inventory.  Runs workflow.create() (full parse + filter +
 candidate freeze) so that indexed_candidates.csv is produced by the same
 machinery that finds start/end labels, then extracts symbols from it.
 Skips batch/action-desc/delivery-table/trace/coverage.  Reads only source
 XML, never GT.  Outputs <proc_name>_步序变量表.xlsx + step_variables.json/csv."""
 out.mkdir(parents=True,exist_ok=True)
 stats={'mode':'tagvars','source':str(src),'out':str(out),'status':'running'}
 _check_isolation(out,args,'-t')
 # 1) Run the normal candidate pipeline (parse → filter → freeze candidates).
 #    force=True so workflow.create does not refuse on a freshly-cleaned dir.
 manifest=create(str(src),str(out),batch_size=args.batch_size,
                 pattern=args.pattern,force=True,manual=None,trace=None)
 stats['candidate_count']=manifest.get('candidate_count',0)
 # 2) Extract variable docs + station identity (for workstation naming).
 #    -t mode does not need step_comments/network_comments (no descriptions).
 _extract_source_evidence(src,out,include_comments=False)
 # 3) Extract step-variable symbols from indexed_candidates.csv + render xlsx.
 try:
  from gen_stepvars_xlsx import generate as gen_sv_xlsx
  xlsx_stats=gen_sv_xlsx(str(out),source=str(src))
  stats.update(xlsx_stats);stats['status']='extracted'
 except Exception as exc:
  stats['error']=str(exc);stats['status']='failed'
  print(json.dumps(stats,ensure_ascii=False,indent=2));sys.exit(2)
 print(json.dumps(stats,ensure_ascii=False,indent=2))

_TRACE_PREPARED = 'trace_prepared.json'
_TRACE_STATS = ('variables_total', 'variables_active', 'total_jumps', 'cycles_detected',
                'unique_pairs', 'pairs_with_action_vars', 'pairs_with_step_comment',
                'pairs_reset_exit', 'review_count', 'source_relations', 'proc_name', 'trace_input')


def _trace_inputs(src, trace, args):
 from trace_to_pairs import trace_input_digest
 from trace_description_context import SCHEMA
 root = Path(__file__).resolve().parent
 code = sorted(root.glob('trace_*.py')) + [
     root/name for name in ('VERSION', 'plc_cli.py', 'workflow.py', 'action_describe.py', 'action_wordbook.json')
 ]
 return {
     'source': str(src), 'source_sha256': fingerprint(src),
     'trace': str(trace), 'trace_sha256': trace_input_digest(trace),
     'description_schema': SCHEMA, 'code': {p.name: fingerprint(p) for p in code},
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
 if src.is_relative_to(out) or trace.is_relative_to(out):
  raise ValueError('输出目录须与PLC源码和Trace输入分开')
 out.mkdir(parents=True,exist_ok=True)
 cache_path = out/_TRACE_PREPARED
 inputs = _trace_inputs(src, trace, args)
 cached = None
 if cache_path.is_file() and not args.force:
  try:
   cached = json.loads(cache_path.read_text(encoding='utf-8'))
   if (cached['inputs'] != inputs or cached['files'] != _trace_prepared_files(out)
       or not set((*_TRACE_STATS, 'candidate_count')) <= cached['stats'].keys()
       or (out/'final_prediction_pairs.csv').exists()):
    raise ValueError('输入或准备产物已变化')
  except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
   raise SystemExit(f'Trace准备结果无法复用（{exc}）。请用新目录或显式 --force 重建。') from exc
 else:
  _check_isolation(out,args,'trace')
 stats = {'mode':'trace','source':str(src),'trace':str(trace),'out':str(out),
          'preparation_reused': cached is not None}
 stage = 'source'
 try:
  if cached is None:
   print('Trace [1/4] 解析 PLC 源码并准备候选批次', file=sys.stderr, flush=True)
   manifest = create(str(src),str(out),batch_size=args.batch_size,
                     pattern=args.pattern,force=True,manual=None,trace=None)
   stats['candidate_count'] = manifest.get('candidate_count',0)

   stage = 'source_evidence'
   print('Trace [2/4] 提取变量文档、步注释和工位身份', file=sys.stderr, flush=True)
   _extract_source_evidence(src,out,include_comments=True)

   stage = 'trace'
   print('Trace [3/4] 解析采集数据并汇总状态对', file=sys.stderr, flush=True)
   from trace_to_pairs import generate as gen_trace_pairs
   trace_stats = gen_trace_pairs(str(trace),str(out),source=str(src),pattern=args.pattern,
                                 proc_name=args.proc_name,
                                 sample_interval_ms=args.sample_interval_ms)
   stats.update({k: trace_stats[k] for k in _TRACE_STATS})
   stage = 'description'
   print('Trace [4/4] 准备动作描述批次', file=sys.stderr, flush=True)
   from action_describe import cmd_a, cmd_build
   cmd_a(str(out))
   cmd_build(str(out))
   if _trace_inputs(src, trace, args) != inputs:
    raise ValueError('准备过程中源码、Trace或程序发生变化，请重建')
   prepared = {'inputs': inputs, 'files': _trace_prepared_files(out),
               'stats': {k: stats[k] for k in (*_TRACE_STATS, 'candidate_count')}}
   tmp_path = cache_path.with_suffix('.tmp')
   tmp_path.write_text(json.dumps(prepared, ensure_ascii=False, indent=2), encoding='utf-8')
   tmp_path.replace(cache_path)
  else:
   print('Trace 续跑：准备产物已核验，合并 B 档结果并刷新交付表', file=sys.stderr, flush=True)
   stats.update(cached['stats'])

  stage = 'description'
  from action_describe import cmd_validate
  cmd_validate(str(out), stats=stats)

  stage = 'workbook'
  from gen_workstation_xlsx import generate as gen_ws
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
 status_path = out/'workflow_status.json'
 tmp_path = status_path.with_suffix('.tmp')
 tmp_path.write_text(status,encoding='utf-8')
 tmp_path.replace(status_path)
 print(status)
 if 'error' in stats:
  sys.exit(2)

def predict(args):
 src=Path(args.source).resolve()
 if not src.is_file():raise FileNotFoundError(f'PLC源码不存在：{src}')
 if src.suffix.lower() not in ('.xml','.smc2','.txt'):raise ValueError('源码需为XML/SMC2容器或其文本导出')
 out=Path(args.out).resolve()
 # Deliberately no --gt argument or gold-file discovery in prediction.
 if out==src.parent or out==src:raise ValueError('输出必须位于独立目录，不能覆盖源码所在目录')
 # v9.26: --tagvars/-t branch — reuse predict's candidate pipeline to
 # extract the step-variable inventory, skip batch/delivery-table.
 # Reads only source XML, never GT.
 if getattr(args,'tagvars',False):
  return _run_tagvars(src,out,args)
 # v9.23: optional --trace argument (runtime trace file for duration backfill).
 trace_path=args.trace
 if trace_path:
  tp=Path(trace_path).resolve()
  if not tp.is_file():raise FileNotFoundError(f'trace文件不存在：{tp}')
  trace_path=str(tp)
 manifest=create(str(src),str(out),batch_size=args.batch_size,pattern=args.pattern,force=args.force,manual=args.manual,trace=trace_path)
 # Extract source-derived evidence (variable docs + network comments + step comments).
 # station_identity is extracted separately because predict needs si_audit for candidate_audit.json.
 _extract_source_evidence(src,out,include_comments=True,include_station_identity=False)
 # v9.17: build station identity map from candidate controllers + var docs;
 # produces station_identity.json and an audit segment in candidate_audit.json.
 si_audit={'total_var_roots':0,'with_documentation':0,'without_documentation':0,'without_documentation_roots':[]}
 try:
  from station_identity import build_identity
  si_map,si_audit=build_identity(str(out))
  (out/'station_identity.json').write_text(json.dumps(si_map,ensure_ascii=False,indent=1),encoding='utf-8')
  if si_audit['without_documentation_roots']:
   print(f'station_identity: {len(si_audit["without_documentation_roots"])} 个变量根无文档 -> review',file=sys.stderr)
 except Exception as exc:
  print(f'station_identity 构建失败（不影响预测）：{exc}',file=sys.stderr)
 try:
  ap_=json.loads((out/'candidate_audit.json').read_text(encoding='utf-8'))
  ap_['station_identity']=si_audit
  (out/'candidate_audit.json').write_text(json.dumps(ap_,ensure_ascii=False,indent=1),encoding='utf-8')
 except Exception:
  pass
 prov={'version':'9.28.0','source_format':json.loads((out/'candidate_audit.json').read_text(encoding='utf-8'))['format'],'pipeline_order':['source_parse_all','source_cross_fb_protocols','name_filter','boundary_normalization','action_signal_tail_composition','optional_process_review'],'mode':'prediction','source_sha256':fingerprint(src),'source_name':src.name,'pattern':args.pattern,'candidate_sha256':fingerprint(out/'sequence_candidates.csv'),'prediction_has_gt_access':False,'status':'prepared_needs_opencode_batches','workflow_manifest':'workflow_manifest.json','trace':trace_path}
 prov['atomic_output_sha256']=fingerprint(out/'prediction_collection_pairs.csv')
 prov['atomic_archive_sha256']=fingerprint(out/'prediction_atomic_pairs.csv')
 prov['full_pair_archive_sha256']=fingerprint(out/'prediction_candidate_pairs.csv')
 (out/'prediction_provenance.json').write_text(json.dumps(prov,ensure_ascii=False,indent=2),encoding='utf-8')
 # v9.24: generate A-tier descriptions + B-tier batches in predict phase
 # (previously done in finish, requiring two finish runs).  Now predict
 # produces action_desc_a.csv + batches_desc/*.input.json, so agent can
 # process B-tier batches immediately, and finish only needs cmd_validate.
 try:
  from action_describe import cmd_a, cmd_build
  cmd_a(str(out))
  cmd_build(str(out))
 except Exception as exc:
  print(f'动作描述A档/B档批次生成失败（不影响预测）：{exc}',file=sys.stderr)
 audit=json.loads((out/'candidate_audit.json').read_text(encoding='utf-8'))
 with (out/'prediction_candidate_pairs.csv').open(encoding='utf-8-sig',newline='') as f:
  import csv
  complete_pairs=sum(1 for _ in csv.DictReader(f))
 print(json.dumps({'mode':'predict','source_format':audit['format'],'output':str(out),'complete_candidate_pairs':complete_pairs,'main_prediction':str(out/'prediction_collection_pairs.csv'),'full_recall_archive':str(out/'prediction_candidate_pairs.csv'),'atomic_projection':json.loads((out/'prediction_atomic_summary.json').read_text(encoding='utf-8')),'batch_count':len(manifest['batches']),'next':'OpenCode逐批写result.json后运行 python plc_cli.py finish --run "目录"'},ensure_ascii=False,indent=2))

def finish(args):
 r=validate(args.run,write=True)
 print(json.dumps(r,ensure_ascii=False,indent=2))
 if not r['success']:sys.exit(2)

def coverage(args):
 """v9.19: coverage evaluation against a manual collection workbook.
 Wraps 评价器/gen_coverage_eval.py — reads prediction + manual, never modifies either."""
 prediction=Path(args.prediction).resolve()
 manual=Path(args.manual).resolve()
 if not prediction.is_file():raise FileNotFoundError(f'预测文件不存在：{prediction}')
 if not manual.is_file():raise FileNotFoundError(f'人工表不存在：{manual}')
 out=Path(args.out).resolve()
 out.parent.mkdir(parents=True,exist_ok=True)
 if out==prediction.parent or out==manual.parent:raise ValueError('评价输出请单独指定目录')
 import sys as _sys
 _sys.path.insert(0,str(Path(__file__).resolve().parent/'评价器'))
 import gen_coverage_eval as gce
 proc_name=args.proc_name or gce.infer_proc_name(manual)
 kind=args.kind or gce.infer_kind(manual,'auto')
 var_desc=args.var_desc or 'STxx.AutoStep'
 # call the engine directly (it prints stats + saves xlsx)
 gce.main_with(prediction,manual,out,proc_name,var_desc,kind)
 print(f'覆盖率评价完成: {out}')

def evaluate(args):
 prediction=Path(args.prediction).resolve();truth=Path(args.gt).resolve()
 if not prediction.is_file() or not truth.is_file():raise FileNotFoundError('预测文件或GT不存在')
 out=Path(args.out).resolve()
 if out==prediction.parent or out==truth.parent:raise ValueError('评估输出请单独指定一个目录，避免覆盖预测或GT')
 relations=args.relations
 strict_graph=None
 transition_graph=getattr(args,'transition_graph',None)
 if not transition_graph and not getattr(args,'xml',None):
  sidecar=prediction.parent/'source_transition_graph.json'
  if sidecar.is_file():transition_graph=str(sidecar)
 if transition_graph:
  from boundary_normalizer import load_graph
  if Path(transition_graph).resolve() in (prediction,truth):raise ValueError('迁移图不得与预测/GT相同')
  strict_graph=load_graph(transition_graph)
 xml=getattr(args,'xml',None)
 if xml and transition_graph:raise ValueError('--xml 与 --transition-graph 二选一')
 if xml and relations:raise ValueError('--xml 和 --relations 请二选一，防止混用证据来源')
 if xml:
  xml_path=Path(xml).resolve()
  if not xml_path.is_file():raise ValueError('评价源码路径不存在')
  with xml_path.open('rb') as f:head=f.read(4096).lstrip(b'\xef\xbb\xbf \t\r\n')
  if not head.startswith(b'<'):raise ValueError('评价源码必须为XML文本容器；扩展名可以是.xml/.txt/.smc2')
  if xml_path in (prediction,truth):raise ValueError('XML不能与预测/GT文件相同')
  from successor_graph import write_graph as xml_scan
  evidence_dir=out/'xml_evidence';evidence_dir.mkdir(parents=True,exist_ok=True)
  strict_graph=xml_scan(xml_path,evidence_dir)
 if strict_graph is not None:
  provenance=prediction.parent/'prediction_provenance.json'
  if provenance.is_file():
   expected=json.loads(provenance.read_text(encoding='utf-8')).get('source_sha256')
   if expected and expected!=strict_graph.get('source_sha256'):raise ValueError('预测源码SHA256与评价迁移图来源不一致，拒绝混用工程')
 # Never build relations from GT; accept only explicit prediction-produced sidecar.
 if relations:
  rel=Path(relations).resolve()
  if not rel.is_file():raise FileNotFoundError(rel)
  if rel==truth:raise ValueError('关系文件不能与GT相同')
 from table_io import prediction_records
 sample=prediction_records(prediction)
 if sample and '原子对ID' in sample[0]:
  from atomic_evaluation import run as evaluate_atomic
  result=evaluate_atomic(str(prediction),str(truth),str(out),strict_graph,args.top_k)
 else:
  result=evaluate_candidates(str(prediction),str(truth),str(out),relations,args.top_k,graph=strict_graph)
 report={'mode':'evaluation','prediction_sha256':fingerprint(prediction),'gt_sha256':fingerprint(truth),'relations_sha256':fingerprint(relations) if relations else None,'evaluation_xml_sha256':fingerprint(xml) if xml else None,'transition_graph_sha256':fingerprint(transition_graph) if transition_graph else None,'gt_used_only_for_evaluation':True,'categories_not_all_successful':True}
 (out/'evaluation_provenance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 if not args.no_excel:
  try:
   from evaluation_excel import write_excel
   write_excel(out/'gt_match_summary.csv',out/'gt_match_hits.csv',out/'gt_match_metrics.json',out/'gt_match.xlsx')
   print('Excel:',out/'gt_match.xlsx')
  except ImportError as exc:
   print(f'Excel依赖未安装（CSV/JSON已生成）：{exc}',file=sys.stderr)
 print('评估文件：',out)
 return result

def main():
 p=argparse.ArgumentParser(description='PLC Agent v9.28，候选保全、逻辑后继统一、动作描述纯中文化(A档步注释自动+B档LLM补充/变量文档注入/简短动作重点)、predict生成A档+B档(finish一次完成)、预测评价隔离、trace跳变分析与时长回填(--trace)、覆盖评价(--manual自动触发)、步序变量清单提取(--tagvars/-t短路模式)、STCode状态比较补充轨道、trace步序采集模式(trace子命令)')
 sub=p.add_subparsers(dest='mode',required=True)
 x=sub.add_parser('predict',help='源码+变量模式；不接受GT')
 x.add_argument('--source',required=True);x.add_argument('--pattern');x.add_argument('--out',required=True);x.add_argument('--batch-size',type=int,default=120);x.add_argument('--force',action='store_true'); x.add_argument('--manual',default=None,help='可选：人工采集表路径，传入后finish自动运行覆盖评价');x.add_argument('--trace',default=None,help='可选：运行时trace文件路径(.trace)，传入后finish自动分析跳变并回填交付表时长列');x.add_argument('--tagvars','-t',action='store_true',help='步序变量提取模式：跳过候选/批次/交付表，只输出<工序名>_步序变量表.xlsx');x.set_defaults(func=predict)
 x=sub.add_parser('evaluate',help='已有预测+GT；可读取XML独立核验，不修改预测')
 x.add_argument('--prediction',required=True);x.add_argument('--gt',required=True);x.add_argument('--out',required=True);x.add_argument('--relations');x.add_argument('--transition-graph',help='预测阶段生成的source_transition_graph.json，不接受GT作为证据');x.add_argument('--xml',help='仅评价读取PLCopen/SMC2/欧姆龙原生IEC61131-10 XML文本，生成独立控制流证据，绝不回写预测');x.add_argument('--top-k',type=int,default=5);x.add_argument('--no-excel',action='store_true');x.set_defaults(func=evaluate)
 x=sub.add_parser('finish',help='组装所有OpenCode批次+自动A档动作描述+交付表');x.add_argument('--run',required=True);x.set_defaults(func=finish)
 x=sub.add_parser('coverage',help='覆盖对比评价（有人工表时用gen_coverage_eval）')
 x.add_argument('--prediction',required=True);x.add_argument('--manual',required=True);x.add_argument('--out',required=True);x.add_argument('--proc-name',default=None);x.add_argument('--var-desc',default=None);x.add_argument('--kind',default=None);x.set_defaults(func=coverage)
 x=sub.add_parser('trace',help='trace步序采集模式：trace跳变→状态对→A/B档动作描述→交付表')
 x.add_argument('--source',required=True,help='PLC源码XML路径（用于动作变量映射/步注释/工位身份）')
 x.add_argument('--trace',required=True,help='trace输入：TwinCAT .trace文件 / Sysmac CSV表 / Sysmac采集目录')
 x.add_argument('--sample-interval-ms',type=float,help='Sysmac 单次采集的 RawTime 精度不足时指定采样间隔(ms)')
 x.add_argument('--proc-name',default=None,help='工序名称；缺省优先使用源码所在工序目录名')
 x.add_argument('--out',required=True,help='输出目录')
 x.add_argument('--pattern',default=None,help='可选：变量名称过滤模式（glob或re:前缀正则）')
 x.add_argument('--batch-size',type=int,default=120)
 x.add_argument('--force',action='store_true',help='强制重建准备产物；同一运行合并B档时可直接续跑')
 x.set_defaults(func=lambda a:_run_trace_mode(Path(a.source).resolve(),Path(a.out).resolve(),a))
 a=p.parse_args();a.func(a)
if __name__=='__main__':main()
