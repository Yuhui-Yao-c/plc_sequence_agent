"""Output isolation and provenance for a single Trace run."""
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from source_format import detect_source_format


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def pipeline_fingerprint():
    root = Path(__file__).resolve().parent
    paths = [p for p in root.glob('*.py') if not p.name.startswith('test_')]
    paths += list((root / '.opencode').glob('agents/*.md'))
    paths += list((root / '.opencode').glob('skills/*/SKILL.md'))
    paths += [root / name for name in ('VERSION', 'AGENTS.md', 'opencode.json', 'requirements.txt')]
    hashes = {p.relative_to(root).as_posix(): fingerprint(p) for p in paths if p.is_file()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()


@contextmanager
def lock_run_dir(out):
    """Serialize commands targeting the same output, including rebuilds."""
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with (out.parent / f'.{out.name}.plc.lock').open('a+b') as lock:
        try:
            if os.name == 'nt':
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise SystemExit(f'输出目录正在由其他工作流命令使用：{out}') from exc
        yield


def _fresh_output(out, force):
    if out.exists() and not out.is_dir():
        raise ValueError(f'输出路径须为目录：{out}')
    if not out.exists() or not any(out.iterdir()):
        out.mkdir(parents=True, exist_ok=True)
        return None
    if not force:
        raise SystemExit(f'新一轮执行须使用空目录：{out}。如需在原路径重建，可用 --force 归档旧产物。')
    markers = ('workflow_manifest.json', 'workflow_status.json', 'trace_collection_pairs.csv')
    if not any((out / name).is_file() for name in markers):
        raise SystemExit(f'目录非空且未识别到工作流产物：{out}。请指定空目录。')
    previous = out.with_name(f'{out.name}.previous-{uuid4().hex[:12]}')
    out.rename(previous)
    try:
        out.mkdir()
    except OSError:
        if not out.exists():
            previous.rename(out)
        raise
    print(f'旧工作流产物已归档到 {previous}', file=sys.stderr)
    return str(previous)


def create(source, out, *, trace, force=False, source_sha256=None, pipeline_sha256=None):
    """Reserve the run and record its input identities before source extraction."""
    out = Path(out).resolve()
    if Path.cwd().resolve().is_relative_to(out) or Path(__file__).resolve().is_relative_to(out):
        raise ValueError('输出目录须独立于工作目录和程序文件')
    for path in (source, trace):
        if any(p.is_relative_to(out) for p in (Path(path).absolute(), Path(path).resolve())):
            raise ValueError(f'输出目录须与输入分开：{path}')
    source_format = detect_source_format(source)
    previous = _fresh_output(out, force)
    meta = {
        'run_id': uuid4().hex, 'mode': 'trace', 'previous_output': previous,
        'pipeline_sha256': pipeline_sha256 or pipeline_fingerprint(),
        'source_sha256': source_sha256 or fingerprint(source),
        'source': str(Path(source).resolve()), 'trace': str(Path(trace).resolve()),
        'extraction': {'format': source_format},
    }
    with (out / 'workflow_manifest.json').open('x', encoding='utf-8') as stream:
        json.dump(meta, stream, ensure_ascii=False, indent=2)
    return meta
