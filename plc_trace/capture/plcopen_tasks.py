"""Read project schedules and resolve the single sampling task of a CODESYS Trace record."""
import math
import re
from pathlib import Path

from lxml import etree


_NUMBER = r'[0-9]+(?:\.[0-9]+)?'
_ISO_TIME = re.compile(rf'P(?:({_NUMBER})D)?(?:T(?:({_NUMBER})H)?(?:({_NUMBER})M)?(?:({_NUMBER})S)?)?', re.I)
_TIME_UNIT_MS = {'d': 86400000, 'h': 3600000, 'm': 60000, 's': 1000, 'ms': 1, 'us': .001, 'ns': .000001}
_IEC_PART = re.compile(rf'({_NUMBER})(ms|us|ns|d|h|m|s)')
_TASK_DATA = 'http://www.3s-software.com/plcopenxml/task'


def _milliseconds(value, unit=''):
    value = value.strip().casefold().replace('μ', 'u')
    unit = unit.strip().casefold().replace('μ', 'u')
    if match := _ISO_TIME.fullmatch(value):
        total = sum(float(n) * _TIME_UNIT_MS[u] for n, u in zip(match.groups(), 'dhms') if n)
    elif re.fullmatch(_NUMBER, value) and unit in _TIME_UNIT_MS:
        total = float(value) * _TIME_UNIT_MS[unit]
    else:
        value = re.sub(r'^(?:l?time|l?t)#', '', value).replace('_', '')
        parts = list(_IEC_PART.finditer(value))
        if not parts or ''.join(m[0] for m in parts) != value:
            return None
        total = sum(float(m[1]) * _TIME_UNIT_MS[m[2]] for m in parts)
    return total if total > 0 and math.isfinite(total) else None


def is_task_declaration(node):
    parent = node.getparent()
    if parent is None:
        return False
    tag = etree.QName(parent).localname
    return tag == 'resource' or (tag == 'data' and parent.get('name') == _TASK_DATA)


def task_declaration(node):
    """Read a standard resource/task or a task exported in the vendor's addData."""
    settings = node.find('./{*}addData/{*}data/{*}TaskSettings')
    attrs = settings.attrib if settings is not None else {}
    kind = attrs.get('KindOfTask', '')
    interval, setting, unit = node.get('interval', ''), attrs.get('Interval', ''), attrs.get('IntervalUnit', '')
    standard_ms, vendor_ms = _milliseconds(interval), _milliseconds(setting, unit)
    period = standard_ms if standard_ms is not None else vendor_ms
    if node.get('single') or (kind and kind.casefold() != 'cyclic'):
        period = None
    if standard_ms is not None and vendor_ms is not None and not math.isclose(standard_ms, vendor_ms):
        period = None
    name = node.get('name', '').strip()
    scope = [parent.get('name') for parent in reversed(list(node.iterancestors()))
             if etree.QName(parent).localname in ('configuration', 'resource') and parent.get('name')]
    for leaf in node.iterfind('./{*}addData/{*}data/{*}pathStructure//{*}pathStructure'):
        if leaf.get('name') == name and not len(leaf):
            scope = [p.get('name') for p in reversed(list(leaf.iterancestors()))
                     if etree.QName(p).localname == 'pathStructure' and p.get('name')]
            break
    return {
        'name': name, 'scope': scope, 'source_line': node.sourceline,
        'object_id': node.findtext('./{*}addData/{*}data/{*}ObjectId', '').strip(),
        'kind': kind, 'single': node.get('single'), 'priority': node.get('priority'), 'interval': interval,
        'settings_interval': setting, 'interval_unit': unit, 'period_ms': period,
        'pou_instances': [{'name': p.get('name', ''), 'type_name': p.get('typeName', '')}
                          for p in node.findall('./{*}pouInstance')],
    }


def validated_task_name(value):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n'):
        raise ValueError('采集任务名称须为非空单行')
    return value


def _stride(value):
    # EveryNCycles is serialized as System.UInt32.
    digits = str(value).strip().lstrip('0')
    if not re.fullmatch(r'[0-9]{1,10}', digits) or int(digits) > 4294967295:
        raise ValueError('EveryNCycles / --every-n-cycles 须为 1 至 4294967295 的整数')
    return int(digits)


def record_settings(configuration):
    records = configuration.findall("./{*}Single/{*}Single[@Name='Record']")
    lists = configuration.findall("./{*}Single/*[@Name='RecordList']")
    if len(records) != 1 or any(len(item) for item in lists):
        raise ValueError('参考 Trace 须包含一个可明确定位的 Record')
    names = records[0].findall("./{*}Single[@Name='TaskName']")
    strides = records[0].findall("./{*}Single[@Name='EveryNCycles']")
    if len(names) != 1 or len(strides) != 1:
        raise ValueError('Trace 记录需要唯一的 TaskName 和 EveryNCycles')
    return validated_task_name((names[0].text or '').strip()), _stride(strides[0].text)


def reference_settings(path):
    """Stop at the configuration boundary, before captured values and timestamps."""
    with Path(path).open('rb') as stream:
        for event, node in etree.iterparse(stream, events=('start', 'end'), no_network=True, resolve_entities=False):
            tag = etree.QName(node).localname
            if node.getparent() is None and tag != 'Trace':
                break
            if node.getparent() is not None and node.getparent().getparent() is None:
                if tag == 'TraceConfiguration' and event == 'end':
                    return record_settings(node)
                if tag == 'TraceData':
                    break
    raise ValueError('参考文件缺少汇川 TraceConfiguration 配置段')


def _one_declaration(matches):
    if not matches:
        return None
    first = matches[0]
    # Some exporters include both resource/task and an addData copy of the same object.
    if len({t['kind'].casefold() for t in matches if t['kind']}) > 1:
        return None
    for other in matches[1:]:
        if not first['object_id'] or first['object_id'].casefold() != other['object_id'].casefold():
            return None
        for field in ('period_ms', 'single', 'priority', 'pou_instances'):
            if other[field] != first[field]:
                return None
    return first


def sampling_task(tasks, defaults, *, task_name, task_from_trace, every_n_cycles):
    """Prefer the requested sampler. Program scheduling alone cannot identify an earlier recording's choice."""
    name, cycles = defaults
    origin, cycle_origin = '模板默认', '模板默认'
    reference = None
    if task_from_trace is not None:
        if task_name is not None:
            raise ValueError('--task-name 和 --task-from-trace 只能指定其一')
        reference = str(Path(task_from_trace).resolve())
        name, cycles = reference_settings(reference)
        origin = cycle_origin = '参考 Trace'
    elif task_name is not None:
        name, origin = validated_task_name(task_name).strip(), '用户指定'
    elif len({t['name'].casefold() for t in tasks}) == 1:
        name, origin = tasks[0]['name'], '源码唯一任务'
    if every_n_cycles is not None:
        cycles, cycle_origin = _stride(every_n_cycles), '用户指定'

    matches = [t for t in tasks if t['name'].casefold() == name.casefold()]
    selected = _one_declaration(matches)
    note = ''
    if selected:
        name = selected['name']
        if origin == '模板默认':
            origin = '模板默认（源码已核对）'
    elif tasks:
        names = '、'.join(dict.fromkeys(t['name'] for t in tasks))
        if len(matches) > 1:
            note = f'源码中有多个同名任务 {name}，需核对目标应用或资源的导出范围'
        else:
            note = f'源码中未找到任务 {name}，可用 --task-name 选择已声明任务：{names}'
        if task_name is not None or reference is not None:
            raise ValueError(note)
        name, origin = None, '待选择'
    else:
        note = 'XML 未提供任务声明，任务名须在采集客户端核对'

    period = selected['period_ms'] if selected else None
    return {
        'task_name': name, 'task_name_source': origin, 'every_n_cycles': cycles,
        'every_n_cycles_source': cycle_origin, 'task_verified_in_xml': selected is not None,
        'nominal_sample_interval_ms': period * cycles if period is not None else None,
        'task_from_trace': reference, 'task_note': note, 'declared_tasks': tasks,
    }
