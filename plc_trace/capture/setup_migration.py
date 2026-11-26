"""Recognize compatible unfinished runs and recompute their scope with the current pipeline."""


def can_migrate_pending(status, out, *, source, digest, automatic, capture_format, task_name,
                        every_n_cycles, reference_sha):
    """Match the saved request. The caller verifies files and recomputes both evidence digests."""
    legacy_list = (status.get('version') == '18.5.1' and 'trace_targets_stage' not in status
                   and status.get('trace_targets') == str(out / 'trace_targets_preview.txt'))
    fixed_list = (status.get('version') == '19.0.0' and status.get('trace_targets_stage') == 'preview'
                  and status.get('trace_targets') == str(out / 'static_variables.txt'))
    if not (automatic and (legacy_list or fixed_list) and status.get('mode') == 'trace_setup'
            and status.get('selection_mode') == 'automatic' and status.get('patterns') == []
            and status.get('status') in {'scope_review_pending', 'scope_recheck_pending'}
            and status.get('out') == str(out) and status.get('source') == str(source)
            and status.get('source_sha256') == digest and status.get('request_sha256')
            and status.get('inventory_sha256') and status.get('activation_sha256')):
        return False
    coverage = status.get('coverage')
    if not isinstance(coverage, dict) or coverage.get('reference_sha256', '') != reference_sha:
        return False

    # The old marker stores the resolved format and the origin of each sampling setting.
    if capture_format == 'auto':
        capture_format = {'omron_iec61131_10': 'sysmac', 'smc2': 'sysmac',
                          'plcopen': 'inovance'}.get(status.get('source_format'))
    previous_task = status.get('task_name') if status.get('task_name_source') == '用户指定' else None
    previous_stride = status.get('every_n_cycles') if status.get('every_n_cycles_source') == '用户指定' else None
    if capture_format == 'inovance' and isinstance(task_name, str) and isinstance(previous_task, str):
        task_name = task_name.strip()
        if status.get('task_verified_in_xml'):
            previous_task, task_name = previous_task.casefold(), task_name.casefold()
    return (capture_format is not None and capture_format == status.get('capture_format')
            and task_name == previous_task and every_n_cycles == previous_stride)
