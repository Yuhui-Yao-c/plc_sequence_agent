"""Select, verify and publish an entrance-two capture run."""
import json
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from plc_trace.runtime.workflow import _fresh_output, fingerprint


def read_status(out):
    marker = out / 'workflow_status.json'
    if marker.is_symlink() or not marker.is_file():
        return {}
    try:
        status = json.loads(marker.read_bytes())
    except (ValueError, FileNotFoundError):
        return {}
    return status if isinstance(status, dict) else {}


def matches_request(status, out, request):
    return (status.get('mode') == 'trace_setup' and status.get('out') == str(out)
            and status.get('request_sha256') == request)


def legacy_pending(status, source, out, digest, task_name, every_n_cycles):
    """An explicitly targeted older pending run can be revalidated from its XML."""
    task = status.get('task_name') if status.get('task_name_source') == '用户指定' else None
    cycles = status.get('every_n_cycles') if status.get('every_n_cycles_source') == '用户指定' else None
    return ('request_sha256' not in status and status.get('status') == 'scope_review_pending'
            and status.get('selection_mode') == 'automatic' and status.get('out') == str(out)
            and status.get('source') == str(source) and status.get('source_sha256') == digest
            and task == task_name and cycles == every_n_cycles and bool(status.get('inventory_sha256')))


def select_run_dir(base, request):
    """Prefer an existing matching run, even if an earlier numbered slot is free."""
    occupied = set()
    stem = os.path.normcase(base.name)
    entries = base.parent.iterdir() if base.parent.is_dir() else ()
    for path in entries:
        name = os.path.normcase(path.name)
        if name == stem:
            index = 1
        elif name.startswith(stem + '_'):
            suffix = name[len(stem) + 1:]
            if not suffix.isascii() or not suffix.isdecimal() or suffix.startswith('0'):
                continue
            index = int(suffix)
            if index < 2:
                continue
        else:
            continue
        if not path.is_symlink() and path.is_dir():
            if matches_request(read_status(path), path, request):
                return path
            if not any(path.iterdir()):
                continue
        occupied.add(index)
    index = 1
    while index in occupied:
        index += 1
    return base if index == 1 else base.with_name(f'{base.name}_{index}')


def verify_files(out, status):
    hashes = status.get('artifact_sha256', {})
    required = {'static_variables.txt'}
    state = status.get('status')
    if state not in {'scope_review_pending', 'needs_review', 'done', 'done_with_review'}:
        raise ValueError('已有采集配置的工作流状态无效，请核对后显式重建')
    if status.get('selection_mode') == 'automatic':
        required.update(('step_variable_discovery.json', 'scope_candidates.jsonl'))
        if status.get('scope_policy') == 'production_timing':
            required.update(('optional_variables.csv', 'capture_context.csv'))
        if state != 'scope_review_pending' and (status.get('scope_candidates') or status.get('single_write_candidates')):
            required.add('scope_results.json')
    if state in {'done', 'done_with_review'}:
        config = Path(status.get('configuration') or '')
        if config.parent != out:
            raise ValueError('已有采集配置的交付路径无效，请核对后显式重建')
        required.add(config.name)
    if not isinstance(hashes, dict) or not required.issubset(hashes):
        raise ValueError('已有采集配置缺少文件校验记录，请显式重建')
    for name, expected in hashes.items():
        path = out / name
        if (Path(name).name != name or path.is_symlink() or not path.is_file()
                or fingerprint(path) != expected):
            raise ValueError(f'已有采集配置文件缺失或已修改：{name}。请核对后显式重建')


def publish(out, files, *, force, completing):
    with TemporaryDirectory(prefix='.trace-setup-', dir=out.parent) as temporary:
        stage = Path(temporary) / 'ready'
        stage.mkdir()
        for name, contents in files.items():
            (stage / name).write_bytes(contents)
        if completing:
            # Keep user files and the run directory. Commit workflow_status.json last.
            backup = Path(temporary) / 'rollback'
            backup.mkdir()
            changed = []
            owned = {'static_variables.txt', 'scope_candidates.jsonl', 'step_variable_discovery.json',
                     'workflow_status.json', 'scope_results.json', 'capture_context.csv',
                     'step_array_coverage.csv', 'unresolved_types.csv', 'optional_variables.csv'}
            try:
                old_files = read_status(out).get('artifact_sha256', {})
                for name in (old_files.keys() & owned) - files.keys():
                    old = out / name
                    if old.is_symlink() or not old.is_file() or fingerprint(old) != old_files[name]:
                        raise ValueError(f'待更新的采集产物缺失或已修改：{old}')
                    old.replace(backup / name)
                    changed.append(name)
                for name in files:
                    target = out / name
                    if target.is_symlink() or (target.exists() and not target.is_file()):
                        raise ValueError(f'目标位置已有其他文件，请核对：{target}')
                    if target.is_file():
                        if fingerprint(target) == fingerprint(stage / name):
                            continue
                        if name not in owned:
                            raise ValueError(f'目标位置已有其他文件，请核对：{target}')
                        if name in {'capture_context.csv', 'step_array_coverage.csv', 'unresolved_types.csv', 'optional_variables.csv'} and name not in old_files:
                            raise ValueError(f'目标位置已有其他文件，请核对：{target}')
                        shutil.copy2(target, backup / name)
                    (stage / name).replace(target)
                    changed.append(name)
            except BaseException:
                for name in reversed(changed):
                    saved = backup / name
                    if saved.exists():
                        saved.replace(out / name)
                    else:
                        (out / name).unlink()
                raise
            return
        previous = _fresh_output(out, force)
        try:
            out.rmdir()
            stage.rename(out)
        except OSError:
            if previous and not out.exists():
                Path(previous).rename(out)
            raise
