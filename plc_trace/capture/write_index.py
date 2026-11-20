"""A source-only cache. Recording events and delivery decisions are never cached here."""
import hashlib
import json
from pathlib import Path

from plc_trace.capture.step_discovery import discover_source_writes
from plc_trace.runtime.workflow import fingerprint, pipeline_fingerprint
from plc_trace.source.source_format import detect_source_format

INDEX_FILE = 'source_branch_index.json'


def write_index(source, run, reuse=None):
    source = Path(source)
    stamp = {'schema': 1, 'source': fingerprint(source), 'pipeline': pipeline_fingerprint()}
    def digest(records):
        data = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(data.encode('utf-8')).hexdigest()
    if reuse:
        cached = json.loads(Path(reuse).read_text(encoding='utf-8'))
        if cached.get('identity') != stamp or cached.get('sha256') != digest(cached.get('records')):
            raise ValueError('源码写点索引与本次源码、解析程序或内容校验不一致')
    else:
        writes, unknown = [], []
        discover_source_writes(source, detect_source_format(source),
                               lambda *args: writes.append(args), lambda *args: unknown.append(args))
        records = {'writes': writes, 'unresolved': unknown}
        cached = {'identity': stamp, 'sha256': digest(records), 'records': records}
    target = Path(run) / INDEX_FILE
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(cached, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    temporary.replace(target)
    return cached['records']
