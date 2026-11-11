#!/usr/bin/env python3
"""Deterministic orchestration/checkpointing for an OpenCode-driven PLC analysis.
No gold labels, project names or specific PLC step constants are used.
"""
import csv,hashlib,json,os,re,sys
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4
from plc_trace import __version__
from plc_trace.paths import PACKAGE_ROOT, PROJECT_ROOT, RESOURCE_ROOT
from plc_trace.source.agent import run as extract
from plc_trace.analysis.candidate_filter import filter_rows,filter_csv
from plc_trace.analysis.boundary_normalizer import META,normalize_rows,load_graph,empty_graph

CANDIDATE_FIELDS=['构成要素','动作','动作描述','开始标签','结束标签','状态','证据POU','证据节点','原始条件','控制器','分支数']+META
def csv_read(path):
 with open(path,encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))
def csv_write(path,rows,fields):
 with open(path,'w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
def norm(s):return re.sub(r'\s+','',str(s or '')).lower()

def pack_controller_batches(crows,evrows,batch_size):
 """Attach each event to a matching candidate, not an unrelated ordinal chunk.
 Overflow batches repeat only referenced evidence rows; the main pool is unchanged.
 """
 if batch_size<1:raise ValueError('batch_size must be positive')
 packs=[{'c':crows[i:i+batch_size],'e':[]} for i in range(0,len(crows),batch_size)]
 bystart=defaultdict(list)
 for i,c in enumerate(crows):bystart[norm(c.get('开始标签',''))].append(i)
 anchors={};unmatched=[]
 for ev in evrows:
  eligible=bystart.get(norm(ev.get('guard','')),[])
  target=norm(ev.get('next_state',''))
  exact=[i for i in eligible if target and norm(crows[i].get('结束标签',''))==target]
  choices=exact or eligible
  if not choices:unmatched.append(ev);continue
  anchor=min(choices,key=lambda i:(0 if any(k in crows[i].get('状态','') for k in ('smc2_wired','native_wired','cross_fb_transport')) else 1,i))
  anchors[ev.get('事件ID',str(id(ev)))]=crows[anchor]
  packs[anchor//batch_size]['e'].append(ev)
 result=[]
 for pack in packs:
  result.append({'c':pack['c'],'e':pack['e'][:batch_size]})
  for i in range(batch_size,len(pack['e']),batch_size):
   es=pack['e'][i:i+batch_size];cs={}
   for ev in es:
    c=anchors.get(ev.get('事件ID',str(id(ev))))
    if c:cs[c['候选ID']]=c
   result.append({'c':list(cs.values()),'e':es})
 for i in range(0,len(unmatched),batch_size):result.append({'c':[],'e':unmatched[i:i+batch_size]})
 return result

def fingerprint(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''):
   h.update(block)
 return h.hexdigest()


def pipeline_fingerprint():
 root = PROJECT_ROOT
 paths = [root / 'plc_cli.py']
 paths += list(PACKAGE_ROOT.rglob('*.py'))
 paths += [p for p in RESOURCE_ROOT.rglob('*') if p.is_file()]
 paths += list((root / '.opencode/agents').glob('*.md'))
 paths += list((root / '.opencode/skills').rglob('*.md'))
 paths += [root / name for name in ('VERSION', 'AGENTS.md', 'opencode.json', 'requirements.txt')]
 hashes = {p.relative_to(root).as_posix(): fingerprint(p) for p in paths if p.is_file()}
 return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


def _check_output_location(out):
 if Path.cwd().resolve().is_relative_to(out) or PROJECT_ROOT.is_relative_to(out):
  raise ValueError('输出目录须独立于工作目录和程序文件')


@contextmanager
def lock_run_dir(out):
 """Serialize commands targeting the same output directory, including rebuilds."""
 out=Path(out).resolve()
 out.parent.mkdir(parents=True,exist_ok=True)
 # A sibling lock stays in place when --force archives the old directory.
 with (out.parent/f'.{out.name}.plc.lock').open('a+b') as lock:
  try:
   if os.name=='nt':
    import msvcrt
    lock.seek(0)
    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
   else:
    import fcntl
    fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
  except OSError as exc:
   raise SystemExit(f'输出目录正在由其他工作流命令使用：{out}') from exc
  yield


def _fresh_output(out, force):
 if out.exists() and not out.is_dir():
  raise ValueError(f'输出路径须为目录：{out}')
 if not out.exists() or not any(out.iterdir()):
  out.mkdir(parents=True,exist_ok=True)
  return None
 if not force:
  raise SystemExit(f'新一轮执行须使用空目录：{out}。如需在原路径重建，可用 --force 归档旧产物。')
 markers=('workflow_manifest.json','workflow_status.json','sequence_candidates.csv',
          'recall_candidates.csv','trace_collection_pairs.csv','action_desc_a.csv')
 if not any((out/name).is_file() for name in markers):
  raise SystemExit(f'目录非空且未识别到工作流产物：{out}。请指定空目录。')
 previous=out.with_name(f'{out.name}.previous-{uuid4().hex[:12]}')
 out.rename(previous)
 try:
  out.mkdir()
 except OSError:
  if not out.exists():
   previous.rename(out)
  raise
 print(f'旧工作流产物已归档到 {previous}',file=sys.stderr)
 return str(previous)


def create(source,out,batch_size=120,force=False,pattern=None,manual=None,trace=None,*,mode='predict'):
 out=Path(out).resolve()
 _check_output_location(out)
 if not Path(source).is_file():
  raise FileNotFoundError(f'PLC源码不存在：{source}')
 for path in (source,manual,trace):
  if path and any(p.is_relative_to(out) for p in (Path(path).absolute(),Path(path).resolve())):
   raise ValueError(f'输出目录须与输入分开：{path}')
 if batch_size<1:
  raise ValueError('batch_size must be positive')
 previous=_fresh_output(out,force)
 run_id=uuid4().hex
 manifest=out/'workflow_manifest.json'
 run_meta={'run_id':run_id,'mode':mode,'previous_output':previous,
           'pipeline_sha256':pipeline_fingerprint(),'source_sha256':fingerprint(source)}
 with manifest.open('x',encoding='utf-8') as stream:
  json.dump({**run_meta,'status':'PREPARING'},stream)
 report=extract(str(source),str(out))
 rows=csv_read(out/'sequence_candidates.csv');events=csv_read(out/'event_graph.csv')
 if pattern:
  filter_summary=filter_csv(out/'sequence_candidates.csv',out/'sequence_candidates_filtered.csv',pattern)
  rows=filter_rows(rows,pattern)
 else:filter_summary={'before':len(rows),'after':len(rows),'removed':0,'pattern':None}
 from plc_trace.analysis.process_semantics import classify_file
 process_triage=classify_file(out/'sequence_candidates_filtered.csv' if pattern else out/'sequence_candidates.csv',out)
 from plc_trace.analysis.temporal_audit import timing_relations,write_csv as write_diag_csv
 time_rows=timing_relations(events)
 write_diag_csv(out/'timing_relations.csv',time_rows,list(time_rows[0]) if time_rows else ['事件源行号','时间关系'])
 (out/'filter_audit.json').write_text(json.dumps(filter_summary,ensure_ascii=False,indent=2),encoding='utf-8')
 for i,r in enumerate(rows,1):r['候选ID']=f'C{i:07d}'
 raw_pairs=[r.copy() for r in rows if r.get('开始标签','').strip() and r.get('结束标签','').strip()]
 csv_write(out/'prediction_candidate_pairs_raw.csv',raw_pairs,['候选ID']+CANDIDATE_FIELDS)
 graph_path=out/'source_transition_graph.json'
 source_graph=load_graph(graph_path) if graph_path.exists() else empty_graph()
 rows,boundary_stats=normalize_rows(rows,source_graph)
 # Recompute source-stage preservation AFTER this workflow's name filter.
 stages=filter_rows(source_graph.get('stage_records',[]),pattern)
 csv_write(out/'source_stage_candidates_filtered.csv',stages,list(stages[0]) if stages else CANDIDATE_FIELDS)
 kept={(norm(r.get('开始标签','')),norm(r.get('结束标签',''))) for r in rows}
 hits=sum((norm(r['开始标签']),norm(r['结束标签'])) in kept for r in stages)
 preservation={'pattern':pattern,'source_stage_rows_in_scope':len(stages),
  'stage_rows_with_pair_in_filtered_candidates':hits,'no_source_stage_pair_lost':hits==len(stages),
  'not_physical_action_coverage':True}
 (out/'source_stage_preservation.json').write_text(json.dumps(preservation,ensure_ascii=False,indent=2),encoding='utf-8')
 csv_write(out/'state_boundary_review.csv',[r for r in rows if r['边界规范化状态'].startswith('待核验')],['候选ID']+CANDIDATE_FIELDS)
 for i,r in enumerate(events,1):r['事件ID']=f'E{i:07d}'
 csv_write(out/'indexed_candidates.csv',rows,['候选ID']+CANDIDATE_FIELDS)
 evfields=['事件ID']+list(events[0] if events else [])
 csv_write(out/'indexed_events.csv',events,evfields)
 # Group first by controller; non-state events remain in a separate auditable bucket.
 groups=defaultdict(lambda:{'c':[],'e':[]})
 controllers=list(dict.fromkeys(r['控制器'].strip() for r in rows if r['控制器'].strip()))
 # A large controller set can exceed the regular-expression cache.
 guard_patterns = [
  (controller, re.compile(r'(?<![\w.\[\]])' + re.escape(controller) + r'\s*='))
  for controller in controllers
 ]
 for r in rows:groups[r['控制器'].strip() or '__ungrouped__']['c'].append(r)
 # Associate events to controllers using exact symbol boundaries in guards; may match more than one.
 # Events without any state guard still need their own batches and must not be silently dropped.
 for e in events:
  guard=e.get('guard','')
  # Source-backed cross-FB intervals start at a BOOL, so attach evidence to the
  # receiver controller named in the endpoint instead of orphaning it.
  if e.get('command')=='cross_fb_transport':guard=guard+' OR '+e.get('next_state','')
  matches = [
   controller for controller, pattern in guard_patterns if pattern.search(guard)
  ] if guard else []
  if matches:
   for c in matches:groups[c]['e'].append(e)
  else:groups['__ungrouped_events__']['e'].append(e)
 batches=[];bid=0; bdir=out/'batches';bdir.mkdir(exist_ok=True)
 for controller,data in sorted(groups.items()):
  crows=data['c'];evrows=data['e']
  if not crows and not evrows:continue
  # Match stage events to their own candidate before applying context limits.
  for pack in pack_controller_batches(crows,evrows,batch_size):
   bid+=1;batch_id=f'B{bid:05d}'
   cp=pack['c'];ep=pack['e']
   payload={'run_id':run_id,'batch_id':batch_id,'controller':controller,'candidate_count':len(cp),'event_count':len(ep),
    'candidates':cp,'events':ep,'task':'SMC2 state_wait代表源网络中的等待阶段，无机械线圈不等于无工艺意义。首先保留source_stage_candidates.csv中的有条件等待与交接，并解释其起止，不将注释中的数字当成新步号。为真实动作/加工保持/检测/工艺等待/关键交接填写过程类型、统计对象、起点含义、终点含义、工艺依据、边界依据。状态退出按源码后继规范化；不得按数字邻接猜测。动作紧接信号/数据收尾时按signal_merge_lineage.json合并；原始分段留档，内部延时计入；不得吞掉下一独立动作或等待。不能确定时记录待核验，不删除主候选。每个事件须入表或unresolved_events。'}
   payload['task']+='结果原样回传本批次 run_id 和 batch_id。'
   (bdir/f'{batch_id}.input.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
   batches.append({'batch_id':batch_id,'controller':controller,'input':f'batches/{batch_id}.input.json','output':f'batches/{batch_id}.result.json','candidate_ids':[r['候选ID'] for r in cp],'event_ids':[r['事件ID'] for r in ep]})
 # A deterministic lossless candidate-pair export BEFORE the optional OpenCode semantic stage.
 # Do not drop weak hypotheses until after recall evaluation; retain provenance.
 pair_rows=[r for r in rows if r.get('开始标签','').strip() and r.get('结束标签','').strip()]
 csv_write(out/'prediction_candidate_pairs.csv',pair_rows,['候选ID']+CANDIDATE_FIELDS)
 from plc_trace.analysis.atomic_boundaries import export_atomic
 atomic_stats=export_atomic(raw_pairs,source_graph,out)
 from plc_trace.analysis.signal_tail_merge import export_collection
 collection_stats=export_collection(out,source_graph)
 # Extraction starts unfiltered; the public audit must describe the actual
 # post-filter files rather than retaining the temporary unfiltered projection.
 report['unfiltered_projection_before_workflow_filter']={
  'atomic_projection':report.get('atomic_projection'),
  'collection_projection':report.get('collection_projection')}
 report['filter']=filter_summary;report['atomic_projection']=atomic_stats
 report['collection_projection']=collection_stats
 (out/'candidate_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


 meta={**run_meta,'version':__version__,'collection_projection':collection_stats,'atomic_projection':atomic_stats,'boundary_normalization':boundary_stats,'source':str(Path(source).resolve()),'manual':str(Path(manual).resolve()) if manual else None,'trace':str(Path(trace).resolve()) if trace else None,'extraction':report,'batches':batches,'batch_size':batch_size,'candidate_count':len(rows),'candidate_pairs_count':len(pair_rows),'event_count':len(events),'status':'PREPARED','filter':filter_summary,'process_triage':process_triage,'plcopen_action_trace':report.get('plcopen_action_trace'),'timing_relation_hypotheses':len(time_rows)}
 (out/'RESULT_SCHEMA.md').write_text('''# 每批结果JSON协议\n```json\n{"run_id":"复制本批输入的run_id","batch_id":"B00001","steps":[{"构成要素":"...","动作":"...","动作描述":"...","开始标签":"...","结束标签":"...","核验状态":"confirmed_or_candidate","证据候选ID":["C0000001"],"证据事件ID":["E0000001"],"说明":"..."}],"unresolved_events":[{"event_id":"E0000002","reason":"无法确定使能或反馈"}],"notes":"..."}\n```\n\n要求：开始标签必须来自候选开始标签（或明确写出证据候选ID）；结束标签必须来自候选结束标签；特殊的新条件只能标记 `source_review_required` 并注明源码位置，不能无证据认定已验证。每个事件ID必须出现在某个steps.证据事件ID或unresolved_events中；不可只凭同网络共现证明因果。每条step还要填写：过程类型（机械动作/加工保持/检测测量/工艺等待/交接数据/周期汇总/内部辅助/未分类）、统计对象、起点含义、终点含义、工艺依据、边界依据。缺字段仅进入复核队列，不能删主候选。\n''',encoding='utf-8')
 tmp=manifest.with_suffix('.tmp')
 tmp.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
 tmp.replace(manifest)
 return meta
