"""Build and publish a complete offline graph for one immutable input pair."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from plc_trace import __version__
from plc_trace.graph.model import build
from plc_trace.output.utils import infer_proc_name
from plc_trace.paths import RESOURCE_ROOT
from plc_trace.runtime.workflow import fingerprint, lock_run_dir, pipeline_fingerprint
from plc_trace.trace.trace_to_pairs import trace_input_digest

RESOURCE = RESOURCE_ROOT / 'logic_graph'
ARTIFACTS = ('logic_graph.html', 'logic_graph.json.gz')


def render(payload):
    template = (RESOURCE / 'index.html').read_text(encoding='utf-8')
    scripts = '\n'.join((RESOURCE / name).read_text(encoding='utf-8')
                        for name in ('ladder.js', 'lineage.js', 'plant.js', 'viewer.js'))
    return (template.replace('/*__STYLE__*/', (RESOURCE / 'style.css').read_text(encoding='utf-8'))
            .replace('/*__APP__*/', scripts)
            .replace('__PAYLOAD__', base64.b64encode(payload).decode('ascii')))


def status_at(out):
    path = out / 'workflow_status.json'
    if path.is_file() and not path.is_symlink():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            pass
    return {}


def run(args):
    src, trace = Path(args.source).resolve(), Path(args.trace).resolve()
    request = {'source_sha256': fingerprint(src), 'trace_sha256': trace_input_digest(trace),
               'source_name':src.name, 'trace_name':trace.name, 'sample_interval_ms':args.sample_interval_ms,
               'pipeline_sha256':pipeline_fingerprint()}
    if args.out:
        out = Path(args.out).absolute()
    else:
        root = Path(args.output_root or 'outputs').resolve()
        out = root / (infer_proc_name(src) + '_logic_graph')
        if out.exists() and (out.is_symlink() or not out.is_dir() or any(out.iterdir())) and status_at(out).get('request') != request:
            suffix = hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()[:12]
            out = out.with_name(out.name + '_' + suffix)
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise ValueError('图谱输出须为独立目录')
    out = out.resolve()
    if any(path == out or path.is_relative_to(out) for path in (src,trace)) or (trace.is_dir() and out.is_relative_to(trace)):
        raise ValueError('图谱输出须与源码及采集输入目录分开')
    with lock_run_dir(out):
        if out.is_dir() and any(out.iterdir()):
            status = status_at(out)
            if status.get('mode') != 'logic-graph' or status.get('request') != request:
                raise ValueError('输出目录已被其他任务占用，请指定另一图谱目录')
            for name in ARTIFACTS:
                path = out / name
                if path.is_symlink() or not path.is_file() or fingerprint(path) != status.get('artifact_sha256',{}).get(name):
                    raise ValueError('已交付的图谱文件发生变化：' + name)
        else:
            with TemporaryDirectory(prefix='.logic-graph-',dir=out.parent) as tmp:
                stage = Path(tmp) / 'run'
                stage.mkdir()
                graph = build(src, trace, args.sample_interval_ms, lambda s: print(s,flush=True))
                if fingerprint(src) != request['source_sha256'] or trace_input_digest(trace) != request['trace_sha256']:
                    raise ValueError('生成期间输入发生变化，请重新运行')
                graph['inputs'] = request
                blob = gzip.compress(json.dumps(graph, ensure_ascii=False, separators=(',',':'),
                                                allow_nan=False).encode(), compresslevel=6, mtime=0)
                (stage / ARTIFACTS[1]).write_bytes(blob)
                (stage / ARTIFACTS[0]).write_bytes(render(blob).encode())
                status = {'mode':'logic-graph','version':__version__,'status':'done','out':str(out),
                          'request':request,'summary':graph['summary'],
                          'artifact_sha256':{n:fingerprint(stage/n) for n in ARTIFACTS}}
                (stage/'workflow_status.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
                if out.exists():
                    out.rmdir()
                stage.replace(out)
    print(json.dumps(status, ensure_ascii=False))
    return status


def add_arguments(parser):
    parser.add_argument('--source',required=True,help='原始 PLC XML / SMC2 工程')
    parser.add_argument('--trace',required=True,help='实测 .trace 或 Sysmac CSV 文件 / 分卷目录')
    parser.add_argument('--sample-interval-ms',type=float,help='已知的实际采样周期，单位 ms')
    dest = parser.add_mutually_exclusive_group()
    dest.add_argument('--out',help='实际交付子目录')
    dest.add_argument('-output', dest='output_root',help='输出根目录，默认 outputs')
