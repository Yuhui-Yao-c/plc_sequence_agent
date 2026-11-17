"""Read project schedules and resolve the single sampling task of a CODESYS Trace record."""
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from lxml import etree


_NUMBER = r'[0-9]+(?:\.[0-9]+)?'
_ISO_TIME = re.compile(rf'P(?:({_NUMBER})D)?(?:T(?:({_NUMBER})H)?(?:({_NUMBER})M)?(?:({_NUMBER})S)?)?', re.I)
_TIME_UNIT_MS = {'d': 86400000, 'h': 3600000, 'm': 60000, 's': 1000, 'ms': 1, 'us': .001, 'ns': .000001}
_IEC_PART = re.compile(rf'({_NUMBER})(ms|us|ns|d|h|m|s)')
_UINT32_MAX = 2**32 - 1
_BUFFER_WINDOW_MS = 2000
_VENDOR_DATA = 'http://www.3s-software.com/plcopenxml/'


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
    return tag == 'resource' or (tag == 'data' and parent.get('name') == _VENDOR_DATA + 'task')


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
    if not re.fullmatch(r'[0-9]{1,10}', digits) or int(digits) > _UINT32_MAX:
        raise ValueError('EveryNCycles / --every-n-cycles 须为 1 至 4294967295 的整数')
    return int(digits)


@dataclass(eq=False)
class Routine:
    name: str
    program: str
    is_root: bool
    is_program: bool
    calls: set = field(default_factory=set)
    writes: dict = field(default_factory=dict)
    path_owners: set = field(default_factory=set)
    variables: set = field(default_factory=set)

    @classmethod
    def definition(cls, tag, name, pou_type, owner):
        is_root = tag == 'pou'
        return cls(name, name if is_root else owner, is_root,
                   is_root and pou_type.casefold() in ('program', ''))

    def bind_path(self, leaf, definition):
        """Detached actions carry their POU in their own vendor project-tree path."""
        if self.is_root or self.program:
            return
        parent = leaf.getparent()
        owner = None
        while parent is not None and etree.QName(parent).localname == 'pathStructure':
            if owner is None and parent.get('isFolder', '').casefold() != 'true':
                owner = parent.get('name')
            parent = parent.getparent()
        if (owner and parent is not None and parent.get('name') == _VENDOR_DATA + 'pathstructure'
                and parent.getparent() is not None and parent.getparent().getparent() is definition):
            self.path_owners.add(owner)


class ProgramWrites:
    """Index executable definitions. Resolve calls only to unambiguous XML definitions."""

    def __init__(self):
        self.roots = defaultdict(list)
        self.actions = defaultdict(list)

    def add(self, routine):
        if routine.is_root:
            if routine.is_program:
                self.roots[routine.program.casefold()].append(routine)
            return
        owners = {routine.program} if routine.program else routine.path_owners
        if len(owners) == 1:
            owner, = owners
            self.actions[owner.casefold(), routine.name.casefold()].append(routine)

    def reachable(self, program):
        root = self.roots.get(program.casefold(), ())
        if len(root) != 1:
            return {}
        queue = deque([(root[0], program.casefold(), [root[0].name])])
        visited, writes = set(), {}
        while queue:
            routine, owner, path = queue.popleft()
            if routine in visited:
                continue
            visited.add(routine)
            locals_ = routine.variables | self.roots[owner][0].variables
            for variable, line in routine.writes.items():
                root_name = re.split(r'[.\[]', variable, maxsplit=1)[0]
                if root_name not in locals_:
                    writes.setdefault(variable, {'call_path': path, 'xml_line': line})
            for called in sorted(routine.calls - locals_):
                definitions = self.actions.get((owner, called))
                next_owner = owner
                if definitions is None:
                    definitions = self.roots.get(called, ())
                    next_owner = called
                if len(definitions) == 1:
                    callee, = definitions
                    queue.append((callee, next_owner, path + [callee.name]))
        return writes

    def bind(self, tasks, channels):
        """Attach a witness per distinct configured channel to its declaring task."""
        by_name, coverage, cached = defaultdict(list), defaultdict(set), {}
        all_writes = set()
        for task in tasks:
            key = task['name'].casefold()
            by_name[key].append(task)
            evidence = {}
            for instance in task['pou_instances']:
                program = (instance['type_name'] or instance['name']).strip()
                if program not in cached:
                    cached[program] = self.reachable(program)
                for channel, witness in cached[program].items():
                    if channel in channels:
                        evidence.setdefault(channels[channel]['name'], witness)
            task['channel_writes'] = evidence
            coverage[key].update(evidence)
            all_writes.update(evidence)
        return by_name, coverage, all_writes


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


def _runtime_buffer(configuration, record, interval):
    buffer = int(record.findtext("./Single[@Name='BufferEntries']"))
    override = configuration.findtext("./Single/Single[@Name='TraceSettings']/Single[@Name='OverrideRTSBufferSize']")
    buffer_origin = '模板设置'
    # A two-second runtime buffer at the configured cadence, including both endpoints.
    if interval and override == 'False' and _BUFFER_WINDOW_MS / interval <= _UINT32_MAX - 1:
        buffer = max(10, math.ceil(_BUFFER_WINDOW_MS / interval) + 1)
        buffer_origin = '按名义采样间隔计算'
    return buffer, buffer_origin


def sampling_task(tasks, program_writes, channels, configuration, *, task_name, every_n_cycles):
    """Select one sampler for the record using distinct, explicitly written channels."""
    record = configuration.find("./Single/Single[@Name='Record']")
    default_cycles = record.findtext("./Single[@Name='EveryNCycles']")
    cycles = _stride(every_n_cycles if every_n_cycles is not None else default_cycles)
    by_name, coverage, all_writes = program_writes.bind(tasks, channels)

    name, origin, note = None, '待选择', ''
    if task_name is not None:
        name, origin = validated_task_name(task_name).strip(), '用户指定'
    elif len(by_name) == 1:
        name, origin = tasks[0]['name'], '源码唯一任务'
    elif tasks:
        highest = max(map(len, coverage.values()))
        leaders = [key for key, matched in coverage.items() if len(matched) == highest]
        resolved = (_one_declaration(group) for group in by_name.values())
        scopes = {tuple(task['scope']) for task in resolved if task and task['channel_writes'] and task['scope']}
        if highest and len(leaders) == 1 and len(scopes) <= 1:
            name, origin = by_name[leaders[0]][0]['name'], '源码写入通道覆盖'
        else:
            counts = '、'.join(f"{group[0]['name']}（{len(coverage[key])}）" for key, group in by_name.items())
            if not highest:
                reason = '未找到已调度程序对所选通道的明确写入'
            elif len(scopes) > 1:
                reason = '写入证据分属多个应用或资源'
            else:
                reason = '最高写入覆盖数并列'
            note = f'{reason}。各任务覆盖通道数：{counts}'

    selected = None
    if name and tasks:
        matches = by_name.get(name.casefold(), ())
        selected = _one_declaration(matches)
        if selected:
            name = selected['name']
            covered = len(selected['channel_writes'])
            if covered < len(channels):
                note = f'所选任务有 {covered}/{len(channels)} 个通道的静态写入证据。'
            if any(matched for key, matched in coverage.items() if key != name.casefold()):
                note += '有通道由其他任务写入。此记录的全部通道均按所选任务的节拍采样。'
        else:
            note = (f'源码中有多个同名任务 {name}，需核对目标应用或资源' if matches else
                    f"源码中未找到任务 {name}。已声明：{'、'.join(t['name'] for t in tasks)}")
            if task_name is not None:
                raise ValueError(note)
            name, origin = None, '待选择'
    elif not tasks:
        note = ('XML 未提供任务声明，指定的任务名须在采集客户端核对' if name else
                'XML 未提供任务声明，请导出含任务的工程，或以 --task-name 指定已核实的采样任务')

    period = selected['period_ms'] if selected else None
    interval = period * cycles if period is not None else None
    if interval is not None and not math.isfinite(interval):
        interval = None
    buffer, buffer_origin = _runtime_buffer(configuration, record, interval)
    return {
        'task_name': name, 'task_name_source': origin, 'every_n_cycles': cycles,
        'every_n_cycles_source': '用户指定' if every_n_cycles is not None else '模板默认',
        'task_verified_in_xml': selected is not None, 'nominal_sample_interval_ms': interval,
        'buffer_entries': buffer, 'buffer_entries_source': buffer_origin,
        'task_note': note, 'declared_tasks': tasks,
        'channels_without_task_write': [item['name'] for item in channels.values() if item['name'] not in all_writes],
    }
