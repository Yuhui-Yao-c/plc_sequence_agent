"""Format source-backed details for the Trace description report."""

import re
from unicodedata import normalize


_STEP_NUMBER = re.compile(r'^\s*(?:步\s*)?[+-]?\d+\s*[-—:>]\s*')
_STEP_HEADING = re.compile(r'^(?:[^\w\s]|_){2,}\s*[\u3400-\u9fff]{1,3}步\s*(?:[^\w\s]|_){2,}$')
_END_PHASE = re.compile(r'([\u3400-\u9fff]{1,3})(?:完成|结束)')


def _location(data, ref='', *, include_scope=True):
    scope = data.get('scope') or data.get('source_unit') or ''
    routine = data.get('callee_scope') or scope.rsplit('::', 1)[-1]
    line = data.get('callee_line') or data.get('source_line')
    parts = [routine] if routine and include_scope else []
    for label, value in (('网络', data.get('network')), ('XML行', line), ('ST行', data.get('st_line')),
                         ('节点', data.get('callee_node') or data.get('node'))):
        if value:
            parts.append(f'{label}{value}')
    if ref:
        parts.append(ref)
    return f'〔{"，".join(parts)}〕' if parts else ''


def _transition(data):
    target = data.get('to')
    write = data.get('write_value')
    if target is None:
        return f'写步表达式 {write}' if write else '写步目标待核'
    suffix = f'（源码写入 {write}）' if write and str(write) != str(target) else ''
    return f'写入步 {target}{suffix}'


def annotation_supplement(info):
    """Add detail only for short phase notes with a known source scope."""
    text = _STEP_NUMBER.sub('', normalize('NFKC', info['description']).strip())
    heading = _STEP_HEADING.fullmatch(text)
    phase = _END_PHASE.fullmatch(text)
    if not heading and not phase:
        return ''
    refs = set(info.get('description_refs', ()))
    direct = info.get('direct', [])
    origins = [n for n in info.get('start_notes', ()) if n['id'] in refs]
    origins.extend(e for e in direct if e.get('source_id') in refs)
    scopes = {origin.get('scope') for origin in origins}
    if not scopes or None in scopes or '' in scopes:
        return ''
    if phase:
        # The source routine must add a qualifier to the phase's subject.
        # A numbered step or a generic verb by itself gives no such evidence.
        topic = phase[1]
        names = [''.join(re.findall(r'[\u3400-\u9fff]+', scope.rsplit('::', 1)[-1])) for scope in scopes]
        if not any(name.endswith(topic) and len(name) > len(topic) for name in names):
            return ''
    if len(scopes) != 1 or info.get('relation') == 'ambiguous_scope':
        return '例程归属待核'
    scope, = scopes
    routine = scope.rsplit('::', 1)[-1]
    label = f'例程 {routine}'
    path = info.get('candidate_path', []) if not direct else []
    edges = direct or path[:1] or info.get('start_state_writes', [])
    source_ids = {origin.get('source_id') for origin in origins} - {None, ''}
    branches = [edge for edge in edges if edge['scope'] == scope and edge.get('source_id') in source_ids]
    if len(branches) != 1:
        return label + _location(origins[0], origins[0].get('id', ''), include_scope=False)
    branch, = branches
    if path:
        steps = ' → '.join(str(step) for step in [branch['from'], *(edge['to'] for edge in path)])
        label += f'。源码候选路径 {steps}，起始分支{_transition(branch)}'
    else:
        label += f'。本步{_transition(branch)}'
    if guard := branch.get('guard'):
        label += f'。条件 {guard}'
    if branch.get('guard_complete') is False:
        label += '。门控待核'
    return label + _location(branch, include_scope=False)


def _command_text(command, arguments, value=None):
    if value is not None and value != '':
        return f'指令写入 {arguments} := {value}'
    operations = {'coil_set': '置位', 'coil_reset': '复位', 'coil_write': '写入', 'coil_connected': '关联线圈'}
    if command in operations:
        return f'{operations[command]} {arguments}'
    return f'调用 {command}({arguments})'


def _fact_text(item):
    category, data = item['category'], item['data']
    guard = ''
    if category in {'current_command', 'guarded_call', 'related_call'}:
        detail = _command_text(data['command'], data['arguments'], data.get('write_value'))
        if category == 'related_call':
            detail = '关联调用上下文，' + detail
        guard = data.get('guard')
    elif category == 'bound_write':
        detail = f'实参绑定 {data["formal"]} = {data["actual"]}。' + _command_text(data['write'], data['target'])
        guard = data.get('bound_guard')
    elif category in {'step_condition', 'direct_control', 'candidate_path', 'control_context'}:
        role = {'step_condition': '本步条件', 'direct_control': '源码直跳',
                'candidate_path': '源码候选分支', 'control_context': '相邻控制上下文'}[category]
        detail = f'{role}，步 {data["from"]} {_transition(data)}'
        guard = data.get('guard')
    elif category == 'action_anchor':
        detail = f'动作对象 {data["object"]} = {data["actual"]}'
        if doc := data.get('object_documentation'):
            detail += f'。名称依据 {doc}'
    elif category in {'annotated_mapping', 'naming_analogy'}:
        role = '命名类比' if category == 'naming_analogy' else '同对象注释'
        detail = f'{role}，{data["target"]} 参考 {data["example_symbol"]}'
        if example := data.get('example_text') or '、'.join(data.get('mechanism_classes', ())):
            detail += f'「{example}」'
    elif category in {'exact_mapping', 'object_binding'}:
        detail = f'名称依据 {data["symbol"]}「{data["text"]}」'
    elif category in {'start_comment', 'neighbor_comment'}:
        role = '本步注释' if item['local'] else '注释上下文'
        detail = f'{role}「{data["text"]}」'
    elif category == 'timer_alarm':
        detail = f'定时器输出关联报警 {data["target"]}'
    elif category == 'observed_pair':
        return ''  # The Trace pair is already in the report's two label cells.
    else:
        detail = f'源码上下文 {category}'
    if guard:
        detail += f'。条件 {guard}'
    if data.get('guard_complete') is False:
        detail += '。门控待核'
    if item.get('truncated_fields'):
        detail += '。完整字段见源码'
    return detail + _location(data, item['id'])


def action_evidence_text(info, candidate):
    """Resolve each validated clause's references without extending its claims."""
    catalog = {item['id']: item for item in info.get('description_context', {}).get('evidence', ())}
    catalog.update((c['id'], {'id': c['id'], 'category': 'current_command', 'data': c})
                   for c in info.get('b_evidence', ()) if c['id'] not in catalog)
    lines = []
    # Early batches have row-level references only.
    for phrase, refs in candidate.get('clause_evidence') or [('', candidate['refs'])]:
        if phrase:
            lines.append(f'【{phrase}】')
        ordered = {}
        for ref in refs:
            item = catalog.get(ref)
            if item is None:
                continue
            data = item['data']
            dependencies = ('call_ref',) if item['category'] == 'bound_write' else ()
            if item['category'] == 'timer_alarm':
                dependencies = ('timer_ref', 'alarm_ref')
            for key in dependencies:
                if data.get(key) in catalog:
                    ordered[data[key]] = catalog[data[key]]
            ordered[ref] = item
        lines.extend(text for item in ordered.values() if (text := _fact_text(item)))
    return '\n'.join(lines)
