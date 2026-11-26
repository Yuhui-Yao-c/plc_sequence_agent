"""Select, verify and publish an entrance-two capture run."""
import json
import os
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from plc_trace.runtime.workflow import _fresh_output, fingerprint

SCOPE_ACTIONS = {
    'scope_review_pending': 'review_scope',
    'scope_recheck_pending': 'recheck_scope',
}


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
    if state not in SCOPE_ACTIONS.keys() | {'needs_review', 'done', 'done_with_review'}:
        raise ValueError('已有采集配置的工作流状态无效，请核对后显式重建')
    if status.get('selection_mode') == 'automatic':
        required.update(('step_variable_discovery.json', 'scope_candidates.jsonl'))
        required.update(('optional_variables.csv', 'capture_context.csv'))
        if state != 'scope_review_pending' and any(status.get(k) for k in
                ('scope_candidates', 'single_write_candidates', 'distributed_write_candidates')):
            required.add('scope_results.json')
    if state == 'scope_recheck_pending':
        required.update(('scope_recheck.json', 'scope_results.json', 'trace_targets_preview.txt'))
        if status.get('scope_recheck') != str(out / 'scope_recheck.json') or status.get('configuration'):
            raise ValueError('范围补核目录的状态与交付文件不一致，请核对后显式重建')
    for field in ('configuration', 'preview_configuration', 'trace_targets', 'member_type_template'):
        if not status.get(field) and not (field == 'configuration' and state in {'done', 'done_with_review'}):
            continue
        artifact = Path(status.get(field) or '')
        if artifact.parent != out:
            raise ValueError('已有采集配置的交付路径无效，请核对后显式重建')
        required.add(artifact.name)
    if not isinstance(hashes, dict) or not required.issubset(hashes):
        raise ValueError('已有采集配置缺少文件校验记录，请显式重建')
    for name, expected in hashes.items():
        path = out / name
        if (Path(name).name != name or path.is_symlink() or not path.is_file()
                or fingerprint(path) != expected):
            raise ValueError(f'已有采集配置文件缺失或已修改：{name}。请核对后显式重建')


def verify_scope_upgrade(previous, roles, families):
    """A legacy timing claim can be reviewed in place without changing approved work."""
    by_name = {r['name'].casefold(): r for r in roles}
    fields = ('role', 'use', 'reason', 'basis', 'owner_scope', 'dedicated_step_array', 'interface_peer',
              'service_evidence', 'boundaries', 'timing_basis')
    for row in previous['decisions']:
        key = row['name'].casefold()
        if row.get('legacy_boundaries'):
            continue
        updated = by_name.get(key, {})
        if (any(row.get(field) != updated.get(field) for field in fields)
                or row['admission'] == 'captured' and updated.get('admission') != 'captured'):
            raise ValueError(f'补判只能更新缺少独立计时依据的历史判断：{row["name"]}')
    if any(family not in families for family in previous.get('station_families', ())):
        raise ValueError('补判须保留已核实的工位变量族')


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
                     'step_array_coverage.csv', 'unresolved_types.csv', 'optional_variables.csv',
                     'missing_member_types.csv', 'trace_targets_preview.txt', 'scope_recheck.json'}
            try:
                previous = read_status(out)
                old_files = previous.get('artifact_sha256', {})
                for key in ('preview_configuration', 'configuration'):
                    configuration = Path(previous.get(key) or '')
                    if configuration.parent == out and configuration.name in old_files:
                        owned.add(configuration.name)
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
                        if name in {'capture_context.csv', 'step_array_coverage.csv', 'unresolved_types.csv',
                                    'missing_member_types.csv', 'optional_variables.csv', 'scope_recheck.json'} and name not in old_files:
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
