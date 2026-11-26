"""Select, verify and publish an entrance-two capture run."""
import json
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from plc_trace.runtime.workflow import _fresh_output, fingerprint

SCOPE_ACTIONS = {
    'scope_review_pending': 'review_scope',
    'scope_recheck_pending': 'recheck_scope',
}


EVIDENCE_FILES = frozenset({
    'capture_context.csv', 'optional_variables.csv', 'scope_candidates.jsonl', 'scope_results.json',
    'scope_recheck.json', 'step_array_coverage.csv', 'step_variable_discovery.json', 'unresolved_types.csv',
})
ROOT_FILES = {'static_variables.txt', 'missing_member_types.csv', 'workflow_status.json'}
_VENDOR_FILE = re.compile(r'.+_trace_config(?:_preview)?(?:_partial)?\.(?:csv|trace)')


def artifact_name(basename):
    return f'evidence/{basename}' if basename in EVIDENCE_FILES else basename


def recorded_artifact(status, basename):
    """Read a saved result using the manifest of the run that produced it."""
    key = artifact_name(basename)
    return key if key in status.get('artifact_sha256', {}) else basename


def _valid_artifact(name, *, legacy):
    if not isinstance(name, str):
        return False
    if name.startswith('evidence/'):
        return not legacy and name[len('evidence/'):] in EVIDENCE_FILES
    if not name or any(c in name for c in '/\\:\x00'):
        return False
    return (name in ROOT_FILES or bool(_VENDOR_FILE.fullmatch(name))
            or legacy and (name in EVIDENCE_FILES or name == 'trace_targets_preview.txt'))


def _recorded_path(name, status, out):
    legacy = 'trace_targets_stage' not in status
    if name == 'workflow_status.json' or not _valid_artifact(name, legacy=legacy):
        raise ValueError(f'已有采集配置包含无效的产物路径：{name}')
    path = out / name
    if _VENDOR_FILE.fullmatch(name) and str(path) not in (
            status.get('preview_configuration'), status.get('configuration')):
        raise ValueError(f'已有采集配置包含未登记的厂商文件：{name}')
    _check_parent(path, out)
    return path


def _check_parent(path, out):
    parent = path.parent
    if parent != out and (not parent.is_dir() or parent.is_symlink() or parent.resolve() != out / parent.name):
        raise ValueError(f'采集证据目录缺失或路径无效：{path.parent}')


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


def select_run_dir(base, request, *, compatible=None):
    """Prefer an existing matching run, even if an earlier numbered slot is free."""
    occupied = set()
    compatible_pending = []
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
            status = read_status(path)
            if matches_request(status, path, request):
                return path
            if compatible and compatible(status, path):
                compatible_pending.append((index, path))
            if not any(path.iterdir()):
                continue
        occupied.add(index)
    if compatible_pending:
        return min(compatible_pending)[1]
    index = 1
    while index in occupied:
        index += 1
    return base if index == 1 else base.with_name(f'{base.name}_{index}')


def verify_files(out, status):
    hashes = status.get('artifact_sha256', {})
    legacy = 'trace_targets_stage' not in status
    required = {'static_variables.txt'}
    state = status.get('status')
    if state not in SCOPE_ACTIONS.keys() | {'needs_review', 'done', 'done_with_review'}:
        raise ValueError('已有采集配置的工作流状态无效，请核对后显式重建')
    if status.get('selection_mode') == 'automatic':
        required.update(('step_variable_discovery.json', 'scope_candidates.jsonl',
                         'optional_variables.csv', 'capture_context.csv'))
        if not legacy:
            required.add('step_array_coverage.csv')
        if state != 'scope_review_pending' and any(status.get(k) for k in
                ('scope_candidates', 'single_write_candidates', 'distributed_write_candidates')):
            required.add('scope_results.json')
    if state == 'scope_recheck_pending':
        required.update(('scope_recheck.json', 'scope_results.json'))
        if legacy:
            required.add('trace_targets_preview.txt')
        queue = 'scope_recheck.json' if legacy else artifact_name('scope_recheck.json')
        if status.get('scope_recheck') != str(out / queue) or status.get('configuration'):
            raise ValueError('范围补核目录的状态与交付文件不一致，请核对后显式重建')
    if not legacy:
        required = {artifact_name(name) for name in required}
        stage = 'preview' if state in SCOPE_ACTIONS else 'final'
        if status.get('trace_targets_stage') != stage or status.get('trace_targets') != str(out / 'static_variables.txt'):
            raise ValueError('已有步序清单的阶段或交付路径与任务状态不一致，请核对后显式重建')
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
        path = _recorded_path(name, status, out)
        if path.is_symlink() or not path.is_file() or fingerprint(path) != expected:
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
            if not _valid_artifact(name, legacy=False):
                raise ValueError(f'无效的采集产物路径：{name}')
            staged = stage / name
            staged.parent.mkdir(exist_ok=True)
            staged.write_bytes(contents)
        if completing:
            _update_run(out, files, stage, Path(temporary) / 'rollback')
            return
        previous = _fresh_output(out, force)
        try:
            out.rmdir()
            stage.rename(out)
        except OSError:
            if previous and not out.exists():
                Path(previous).rename(out)
            raise


def _update_run(out, files, stage, backup):
    # Only files recorded in this run may be retired. Preserve raw input and user notes.
    previous = read_status(out)
    old_files = previous.get('artifact_sha256', {})
    historical_pending = ('artifact_sha256' not in previous and 'request_sha256' not in previous
                          and previous.get('status') == 'scope_review_pending')
    backup.mkdir()
    evidence = out / 'evidence'
    created_evidence = False
    changed = []
    try:
        if (stage / 'evidence').is_dir():
            if evidence.is_symlink() or (evidence.exists() and not evidence.is_dir()):
                raise ValueError(f'采集证据目录的目标位置无效：{evidence}')
            if not evidence.exists():
                evidence.mkdir()
                created_evidence = True
        for name in sorted(old_files.keys() - files.keys()):
            old = _recorded_path(name, previous, out)
            if old.is_symlink() or not old.is_file() or fingerprint(old) != old_files[name]:
                raise ValueError(f'待更新的采集产物缺失或已修改：{old}')
            saved = backup / name
            saved.parent.mkdir(exist_ok=True)
            old.replace(saved)
            changed.append(name)
        # Put the marker last so a failed update never advertises new results.
        for name in sorted(files, key=lambda key: key == 'workflow_status.json'):
            target = out / name
            _check_parent(target, out)
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f'目标位置已有其他文件，请核对：{target}')
            if target.is_file():
                digest = fingerprint(target)
                if name in old_files and digest != old_files[name]:
                    raise ValueError(f'待更新的采集产物缺失或已修改：{target}')
                if name not in old_files and name != 'workflow_status.json':
                    # Only the original list remains at the same path in pre-manifest runs.
                    if not (historical_pending and name == 'static_variables.txt'):
                        raise ValueError(f'目标位置已有其他文件，请核对：{target}')
                if digest == fingerprint(stage / name):
                    continue
                saved = backup / name
                saved.parent.mkdir(exist_ok=True)
                shutil.copy2(target, saved)
            (stage / name).replace(target)
            changed.append(name)
    except BaseException:
        for name in reversed(changed):
            saved = backup / name
            if saved.exists():
                saved.replace(out / name)
            else:
                (out / name).unlink()
        if created_evidence:
            evidence.rmdir()
        raise
