"""Find explicit PLC references and prepare channel configurations for a new capture."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

from lxml import etree

from plc_trace import __version__
from plc_trace.paths import PROJECT_ROOT, RESOURCE_ROOT

from plc_trace.analysis.boundary_logic import integer, symbol
from plc_trace.analysis.candidate_filter import glob_regex
from plc_trace.capture.plcopen_tasks import (
    ProgramWrites, Routine, is_task_declaration, sampling_task, task_declaration, validated_task_name,
)
from plc_trace.capture.station_scope import at_path, declaration
from plc_trace.capture.setup_output import (
    legacy_pending, matches_request, publish, read_status, select_run_dir, verify_files,
)
from plc_trace.source.smc2_source import iter_networks
from plc_trace.source.source_format import detect_source_format
from plc_trace.trace.trace_to_pairs import INTEGER_TYPES, REAL_TYPES
from plc_trace.runtime.workflow import fingerprint, lock_run_dir, pipeline_fingerprint


_NAME = r'[^\W\d]\w*'
_ROOT = re.compile(rf'(?<![\w.#]){_NAME}')
_MEMBER_NAME = re.compile(rf'{_NAME}|\d+')
_SUFFIX = re.compile(rf'\s*(?:\.\s*(?:{_NAME}|\d+)|(?P<index>\[))')
_CONTINUATION = re.compile(r'\s*(?:\[|\.(?!\.)|\()')
INSTANCE_STORAGE = {'vars', 'localvars', 'outputvars', 'retainvars'}
_INDEX_EDGE = re.compile(r'[\[\];]')
_SUBSCRIPT = re.compile(r'\[([^\[\]]+)\]')
_ARRAY_TYPE = re.compile(r'ARRAY\s*\[([^\[\]]+)\]\s*OF\s+(.+)', re.I)
_IGNORED = re.compile(
    r'''//[^\r\n]*|\(\*|"(?:\$[\s\S]|""|[^"])*(?:"|\Z)|'(?:\$[\s\S]|''|[^'])*(?:'|\Z)'''
)
_COMMENT_EDGE = re.compile(r'\(\*|\*\)')
_XML_COMMENTS = {'comment', 'comments', 'documentation', 'networkcomment', 'description'}
_EXPRESSION_ATTRIBUTES = {'identifier', 'expression', 'variable', 'var', 'reference'}
_CALL = re.compile(rf'(?P<qualified>\.\s*)?(?<![\w#])(?P<name>{_NAME})\s*\(')
_ST_OPERATOR = re.compile(r'\(|\)|:=|=>')
_OUTPUT_END = re.compile(r'\s*[,)]')


def _executable_text(text, *, keep_positions=False):
    """Keep tokens separated when removing PLC comments and string literals."""
    position = 0
    while match := _IGNORED.search(text, position):
        yield text[position:match.start()]
        position = match.end()
        if match[0] == '(*':
            depth = 1
            for edge in _COMMENT_EDGE.finditer(text, position):
                depth += 1 if edge[0] == '(*' else -1
                if depth == 0:
                    position = edge.end()
                    break
            else:
                return
        if keep_positions:
            yield " " * (position - match.start())
    yield text[position:]


def _constant(text):
    try:
        return integer(text)
    except ValueError:
        return None


def _reference_end(text, end):
    while suffix := _SUFFIX.match(text, end):
        end = suffix.end()
        if not suffix['index']:
            continue
        depth = 1
        for edge in _INDEX_EDGE.finditer(text, end):
            if edge[0] == ';':
                return edge.end(), False
            depth += 1 if edge[0] == '[' else -1
            if depth == 0:
                end = edge.end()
                break
        else:
            return len(text), False
    return end, not _CONTINUATION.match(text, end)


def _channels(part):
    position = 0
    while match := _ROOT.search(part, position):
        position, complete = _reference_end(part, match.end())
        if not complete:
            continue
        name = symbol(part[match.start():position])
        indexes = _SUBSCRIPT.findall(name)
        # An inner bracket denotes an expression, even when its own index is constant.
        if name.count('[') != len(indexes):
            yield name, name, True, match.start(), position
            continue
        values = [tuple(_constant(value) for value in index.split(',')) for index in indexes]
        if any(None in index for index in values):
            yield name, name, True, match.start(), position
            continue
        groups = iter(values)
        canonical = _SUBSCRIPT.sub(lambda _: '[' + ','.join(map(str, next(groups))) + ']', name)
        yield name, canonical, False, match.start(), position



def _st_targets(code):
    """Find assignment ends and FB output argument starts outside strings and comments."""
    assigned, outputs = set(), set()
    depth = 0
    for marker in _ST_OPERATOR.finditer(code):
        op = marker[0]
        if op == '(':
            depth += 1
        elif op == ')':
            depth -= 1
        elif op == ':=' and depth == 0:
            end = marker.start()
            while end and code[end - 1].isspace():
                end -= 1
            assigned.add(end)
        elif op == '=>' and depth > 0:
            start = marker.end()
            while start < len(code) and code[start].isspace():
                start += 1
            outputs.add(start)
    return assigned, outputs


def _xml_output(node, tag, parent):
    if (tag, parent) in {('expression', 'outvariable'), ('variable', 'coil')}:
        return True
    if (tag, parent) != ('expression', 'connectionpointout'):
        return False
    pin = node.getparent().getparent()
    return (pin is not None and _local_name(pin.tag) == 'variable' and pin.getparent() is not None
            and _local_name(pin.getparent().tag) == 'outputvariables')


def _local_name(tag):
    return tag.rsplit('}', 1)[-1].casefold()


def _smc_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() not in _XML_COMMENTS | {'cmt'}:
                yield from _smc_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _smc_values(item)


@dataclass(frozen=True)
class _Frame:
    tag: str
    unit: tuple[str, str] | None
    body: bool
    comment: bool
    type_name: str


class _Selectors:
    """Match any independently compiled selector in one source scan."""

    def __init__(self, patterns, case_sensitive):
        values = [patterns] if isinstance(patterns, str) else patterns
        if not isinstance(values, (list, tuple)) or not values:
            raise ValueError('匹配规则不能为空')
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError('每个匹配规则须为非空字符串')
        self.patterns = list(dict.fromkeys(value.strip() for value in values))
        self.single_pattern = values[0] if len(self.patterns) == 1 else None
        self.matchers = []
        for pattern in self.patterns:
            matcher = glob_regex(pattern)
            if matcher.fullmatch(''):
                raise ValueError('匹配规则可以匹配空名称，请限定所需的步序变量')
            if case_sensitive:
                matcher = re.compile(matcher.pattern, matcher.flags & ~re.IGNORECASE)
            self.matchers.append(matcher)

    def fullmatch(self, value):
        return any(matcher.fullmatch(value) for matcher in self.matchers)


class _SourceScan:
    def __init__(self, matcher, source_format, case_sensitive):
        self.matcher = matcher
        self.source_format = source_format
        self.case_sensitive = case_sensitive
        self.references = {}
        self.dynamic = {}
        self.units = defaultdict(set)
        self.ambiguous_units = set()
        self.directions = defaultdict(set)
        self.declarations = defaultdict(set)
        self.members = defaultdict(set)
        self.declaration_sites = {}
        self.aliases = defaultdict(set)
        self.programs = {}
        self.header = ''
        self.parse_issues = []
        self.tasks = []
        self.program_writes = ProgramWrites()

    def _read(self, text, unit, line, routine=None, *, st=False, output=False):
        if not text or not text.strip():
            return
        parts = _executable_text(text)
        if routine is not None and st:
            # A call or assignment can span comments, strings and multiple physical lines.
            code = ' '.join(parts)
            routine.calls.update(m['name'].casefold() for m in _CALL.finditer(code) if not m['qualified'])
            assigned, outputs = _st_targets(code)
            parts = (code,)
        for part in parts:
            for raw, name, dynamic, start, end in _channels(part):
                names = (name, raw)
                if unit and unit[0] == 'program':
                    names += (f'{unit[1]}.{name}', f'{unit[1]}.{raw}')
                if self.matcher is not None and not any(self.matcher.fullmatch(value) for value in names):
                    continue
                key = name if self.case_sensitive else name.casefold()
                if dynamic:
                    self.dynamic.setdefault(key, name)
                    continue
                self.references.setdefault((key, unit), (name, raw, line))
                if routine is None:
                    continue
                is_target = output and raw == symbol(text)
                if st:
                    is_target = end in assigned or (start in outputs and _OUTPUT_END.match(part, end))
                if is_target:
                    routine.writes.setdefault(name.casefold(), line)

    def read(self, source):
        if self.source_format == 'smc2':
            audit = {}
            for record in iter_networks(source, audit):
                for value in _smc_values(record['net']['CLs']):
                    self._read(value, None, record['source_line'])
            self.parse_issues = audit.get('parse_issues', [])
            return
        frames = [_Frame('', None, False, False, '')]
        task_node = None
        declaration_node = None
        routines = []
        plcopen = self.source_format == 'plcopen'
        with source.open('rb') as stream:
            for event, node in etree.iterparse(stream, events=('start', 'end'), huge_tree=True,
                                               no_network=True, resolve_entities=False):
                if event == 'start':
                    parent = frames[-1]
                    tag, name = _local_name(node.tag), node.get('name', '')
                    unit = (tag, name) if tag in {'pou', 'program', 'functionblock', 'function'} else parent.unit
                    frames.append(_Frame(tag, unit, parent.body or tag in {'body', 'bodycontent'},
                                         parent.comment or tag in _XML_COMMENTS,
                                         name if tag == 'datatypedecl' else parent.type_name))
                    if (self.matcher is None and self.source_format == 'omron_iec61131_10'
                            and declaration_node is None and not parent.body and name):
                        if tag == 'member' and parent.type_name:
                            declaration_node = (node, ('member', parent.type_name.casefold(), name.casefold()))
                        elif tag == 'variable' and parent.tag.endswith('vars') and parent.tag != 'externalvars':
                            owner = None if parent.tag == 'globalvars' else unit
                            declaration_node = (node, ('variable', owner, name.casefold()))
                    if tag == 'fileheader':
                        self.header = ' '.join(node.get(k, '') for k in ('companyName', 'productName')).casefold()
                    if self.matcher is None and tag in {'program', 'function', 'functionblock'} and name:
                        if unit in self.units[name.casefold()]:
                            self.ambiguous_units.add(name.casefold())
                        self.units[name.casefold()].add(unit)
                    if tag == 'program' and name:
                        self.programs[name.casefold()] = unit
                    if plcopen and name and not parent.body and tag in {'pou', 'action', 'method'}:
                        owner = next((r.program for _, r in reversed(routines) if r.program), '')
                        routines.append((node, Routine.definition(tag, name, node.get('pouType', ''), owner)))
                    if tag == 'task' and task_node is None and plcopen and is_task_declaration(node):
                        task_node = node
                    continue
                frame = frames.pop()
                parent = frames[-1]
                if frame.body and not frame.comment:
                    routine = routines[-1][1] if routines else None
                    st = (frame.tag in ('st', 'stcode') or
                          (frame.tag == 'xhtml' and parent.tag in ('st', 'stcode')))
                    self._read(node.text, frame.unit, node.sourceline, routine, st=st,
                               output=routine is not None and _xml_output(node, frame.tag, parent.tag))
                    if routine is not None:
                        if frame.tag == 'block' and not node.get('instanceName') and node.get('typeName'):
                            routine.calls.add(node.get('typeName').casefold())
                        elif frame.tag == 'reference' and parent.tag == 'action' and node.get('name'):
                            routine.calls.add(node.get('name').casefold())
                    for attribute, value in node.attrib.items():
                        if _local_name(attribute) in _EXPRESSION_ATTRIBUTES:
                            self._read(value, frame.unit, node.sourceline)
                if parent.body and not parent.comment:
                    self._read(node.tail, parent.unit, node.sourceline)
                if routines:
                    definition, routine = routines[-1]
                    if (frame.tag == 'variable' and not frame.body and parent.tag.endswith('vars')
                            and parent.tag != 'externalvars' and node.get('name')):
                        routine.variables.add(node.get('name').casefold())
                    if frame.tag == 'pathstructure' and node.get('name') == routine.name:
                        routine.bind_path(node, definition)
                    if node is definition:
                        self.program_writes.add(routine)
                        routines.pop()
                if frame.tag == 'typename' and self.source_format == 'omron_iec61131_10':
                    self._type_name(node, frame)
                if declaration_node is not None:
                    declared, key = declaration_node
                    if node is not declared:
                        continue
                    # Conflicting declarations cannot serve as unit ownership evidence.
                    self.declaration_sites[key] = declaration(node) if key not in self.declaration_sites else None
                    declaration_node = None
                if task_node is not None:
                    if node is not task_node:
                        continue
                    if node.get('name', '').strip():
                        self.tasks.append(task_declaration(node))
                    task_node = None
                node.clear(keep_tail=True)
                while node.getprevious() is not None:
                    del node.getparent()[0]

    def _type_name(self, node, frame):
        value = (node.text or '').strip().upper()
        parent = node.getparent()
        if not value or parent is None:
            return
        if frame.type_name and _local_name(parent.tag) == 'userdefinedtypespec':
            self.aliases[frame.type_name.casefold()].add(value)
        if _local_name(parent.tag) != 'type' or parent.getparent() is None:
            return
        owner = parent.getparent()
        key = owner.get('name', '').casefold()
        group = owner.getparent()
        if not key or group is None:
            return
        tag, group_tag = _local_name(owner.tag), _local_name(group.tag)
        if tag == 'member' and frame.type_name:
            self.members[frame.type_name.casefold(), key].add(value)
        elif tag == 'variable' and group_tag == 'globalvars':
            self.declarations[None, key].add(value)
        elif tag == 'variable' and frame.unit and group_tag != 'externalvars':
            self.declarations[frame.unit, key].add(value)
            if self.matcher is None:
                self.directions[frame.unit, key].add(group_tag)

    def _peel_type(self, values, part):
        """Consume exactly the array dimensions addressed by one member."""
        if len(values) != 1:
            return None, '声明缺失或冲突'
        value, = values
        seen = set()
        if '[' in _SUBSCRIPT.sub('', part):
            return None, '下标包含运行时表达式'
        indexes = iter(_SUBSCRIPT.findall(part))
        pending = next(indexes, None)
        while True:
            key = value.casefold()
            if key in self.aliases:
                if key in seen or len(self.aliases[key]) != 1:
                    return None, '类型别名循环或冲突'
                seen.add(key)
                value, = self.aliases[key]
                continue
            if pending is None:
                return value, ''
            array = _ARRAY_TYPE.fullmatch(value)
            if not array:
                return None, '下标和变量声明不一致'
            bounds, value = array.groups()
            dimensions, supplied = bounds.split(','), pending.split(',')
            if len(dimensions) != len(supplied):
                return None, '下标维数和声明不一致'
            for dimension, index in zip(dimensions, supplied):
                low, sep, high = dimension.partition('..')
                lower, upper = _constant(low), _constant(high) if sep else None
                position = _constant(index)
                if position is None:
                    return None, '下标包含运行时表达式'
                if lower is not None and upper is not None and not lower <= position <= upper:
                    return None, '下标超出声明范围'
            pending = next(indexes, None)

    def member(self, kind, field):
        key = kind.casefold(), field.casefold()
        values = self.members.get(key, ())
        matches = self.units.get(key[0], ())
        if len(matches) == 1 and key[0] not in self.ambiguous_units:
            owner, = matches
            directions = self.directions.get((owner, key[1]), set())
            if owner[0] == 'functionblock' and len(directions) == 1 and directions <= INSTANCE_STORAGE:
                if values:
                    return (), None
                return self.declarations[owner, key[1]], self.declaration_sites.get(('variable', owner, key[1]))
        return values, self.declaration_sites.get(('member', *key))

    def channel_declarations(self, name):
        parts = name.split('.')
        unit = self.programs.get(parts[0].casefold()) if len(parts) > 1 else None
        local = unit and (unit, parts[1].split('[')[0].casefold()) in self.declarations
        start = 1 if local else 0
        root = parts[start].split('[')[0].casefold()
        owner = unit if local else None
        site = self.declaration_sites.get(('variable', owner, root))
        values = self.declarations.get((owner, root), ())
        result = at_path(site, parts, start)
        for i in range(start + 1, len(parts)):
            kind, _ = self._peel_type(values, parts[i-1])
            if not kind:
                break
            values, site = self.member(kind, parts[i].split('[')[0])
            result.extend(at_path(site, parts, i))
        return result

    def sysmac_channel(self, name, unit):
        parts = name.split('.')
        first = _MEMBER_NAME.match(parts[0])[0].casefold()
        if len(parts) > 1 and (program := self.programs.get(parts[0].casefold())):
            local_name = _MEMBER_NAME.match(parts[1])[0].casefold()
            if (program, local_name) in self.declarations:
                unit, parts, first = program, parts[1:], local_name
        local = self.declarations.get((unit, first)) if unit else None
        if local and unit[0] != 'program':
            return name, None, '功能块或函数局部量需要实际实例路径', False
        channel = f'{unit[1]}.{".".join(parts)}' if local else name
        declared = local or self.declarations.get((None, first))
        if not declared:
            return channel, None, '缺少根变量声明', True
        kind, problem = self._peel_type(declared, parts[0])
        for part in parts[1:]:
            if not kind:
                break
            member = _MEMBER_NAME.match(part)[0].casefold()
            kind, problem = self._peel_type(self.member(kind, member)[0], part)
        if kind in INTEGER_TYPES or (self.matcher is None and kind in REAL_TYPES):
            return channel, kind, '', False
        return channel, None, problem or f'采集分析不支持声明类型 {kind}', False


def _sort_key(name):
    return tuple(int(part) if i % 2 else part for i, part in enumerate(re.split(r'(\d+)', name.casefold()))), name


_SYSMAC_SETTINGS = (
    ('TraceType', 'Continuous'), ('TriggerType', ''), ('SamplePeriod', '0:0:10:0:0'),
    ('SampleRate', '1'), ('ExternalFileStorage', 'False'), ('MaxSamplesPerFile', '10000'),
    ('MaxDataSetSize', '100'), ('TargetDirectory', r'C:\OMRON\Data\DataTrace'), ('FilePrefix', ''),
    (), ('SampleTimingType', 'cyclic'), ('TaskName', 'BaseControl'), ('SamplingInterval', '1'),
    ('Triggers', 'False:::'), ('PostTriggerDataRatio', '100'), ('StartOnRun', 'False'),
    ('EnableRealTimeDisplay', 'False'), ('BehaviorWhenMax', 'Stop'), (),
    ('TriggerRecordTime', '0'), ('StartRawTime', '0'),
)
_TRACE_COLORS = (-23296, -16711936, -16776961, -7876885, -65536, -16711681, -256, -8355712, -16776960, -657956)


def _sysmac_config(channels, task):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    for row in _SYSMAC_SETTINGS:
        writer.writerow(('TaskName', task) if row[:1] == ('TaskName',) else row)
    writer.writerow(['Index', 'Date', 'ClockTime', 'RawTime', 'TraceSampID',
                     *(f"{c['name']}:{c['type']}" for c in channels)])
    return stream.getvalue().encode('utf-8-sig')


def _csv_bytes(header, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(header)
    writer.writerows(rows)
    return stream.getvalue().encode('utf-8-sig')


def _capture_context(channels):
    """Keep each distinct enclosing operating condition with a write in that branch."""
    for row in sorted(channels.values(), key=lambda item: _sort_key(item['name'])):
        seen = set()
        for site in row['transitions']:
            guard = site.get('operating_guard')
            source = site['scope'], guard
            if guard and source not in seen:
                seen.add(source)
                yield row['name'], site['scope'], guard, site['source_line'], site.get('st_line', ''), site['id']


def _inovance_config(root, channels, task, source_digest):
    record = root.find(".//Single[@Name='Record']")
    record.find("./Single[@Name='TaskName']").text = task['task_name']
    record.find("./Single[@Name='EveryNCycles']").text = str(task['every_n_cycles'])
    record.find("./Single[@Name='BufferEntries']").text = str(task['buffer_entries'])
    record.find("./Single[@Name='guidPOU']").text = str(uuid5(NAMESPACE_URL, 'plc-trace:' + source_digest))
    variables = record.find("./Single[@Name='VariableList']/List")
    diagrams = record.find("./Single[@Name='Diagrams']/List2")
    samples = root.find('./TraceData/TraceRecord')
    variable, diagram, sample = variables[0], diagrams[0], samples.find('TraceVariable')
    variables.remove(variable)
    diagrams.remove(diagram)
    samples.remove(sample)
    for index, channel in enumerate(channels, 1):
        name = channel['name']
        guid = str(uuid5(NAMESPACE_URL, f'plc-trace:{source_digest}:{name}'))
        v, d, s = deepcopy(variable), deepcopy(diagram), deepcopy(sample)
        v.find("./Single[@Name='VariableName']").text = name
        v.find("./Single[@Name='guid']").text = guid
        v.find("./Single[@Name='GraphColor']").text = str(_TRACE_COLORS[(index - 1) % len(_TRACE_COLORS)])
        d.find(".//Single[@Name='ReferencedVarGuid']").text = guid
        d.find("./Single[@Name='Name']").text = f'Diagram {index}'
        s.set('VarName', name)
        variables.append(v)
        diagrams.append(d)
        samples.append(s)
    ET.indent(root)
    return ET.tostring(root, encoding='utf-16', xml_declaration=True, short_empty_elements=False)


def generate(source, out, pattern=None, *, capture_format='auto', task_name=None, scope_results=None,
             every_n_cycles=None, default_type=None, case_sensitive=False, force=False, choose_available=False):
    source_input = Path(source).absolute()
    source = source_input.resolve()
    automatic = pattern is None
    matcher = None if automatic else _Selectors(pattern, case_sensitive)
    if scope_results is not None and not automatic:
        raise ValueError('用途判断仅用于免填模式')
    if automatic and default_type:
        raise ValueError('自动发现需要源码声明的类型')
    if capture_format not in ('auto', 'sysmac', 'inovance'):
        raise ValueError('采集格式须为 auto、sysmac 或 inovance')
    if default_type and default_type not in INTEGER_TYPES:
        raise ValueError('默认类型须为采集分析支持的整数类型')
    digest = fingerprint(source)
    request = hashlib.sha256(json.dumps({
        'source': str(source), 'source_sha256': digest, 'pipeline': pipeline_fingerprint(),
        'patterns': matcher.patterns if matcher else [], 'case_sensitive': case_sensitive,
        'capture_format': capture_format, 'task_name': task_name, 'every_n_cycles': every_n_cycles,
        'default_type': default_type,
    }, sort_keys=True).encode()).hexdigest()
    out = Path(out)
    if choose_available:
        out = select_run_dir(out.parent.resolve() / out.name, request)
    out = out.resolve()
    if (source_input.is_relative_to(out) or source.is_relative_to(out) or Path.cwd().is_relative_to(out)
            or PROJECT_ROOT.is_relative_to(out)):
        raise ValueError('采集配置输出须使用独立目录，与源码和程序分开')
    with lock_run_dir(out):
        pending = None
        scope_bytes = Path(scope_results).read_bytes() if scope_results is not None else None
        if out.exists() and not out.is_dir():
            raise ValueError('输出路径须为目录')
        if out.exists() and any(out.iterdir()):
            status = read_status(out)
            if status.get('mode') != 'trace_setup':
                raise ValueError('输出目录属于其他任务，请使用空目录')
            if not force:
                legacy = (not choose_available and scope_bytes is not None
                          and legacy_pending(status, source, out, digest, task_name, every_n_cycles))
                if not matches_request(status, out, request) and not legacy:
                    raise ValueError('源码、程序版本或采集选项与已有任务不同，请指定空目录或显式 --force 重建')
                if scope_bytes is not None and status.get('status') == 'scope_review_pending':
                    pending = status
                else:
                    verify_files(out, status)
                    if scope_bytes is not None and hashlib.sha256(scope_bytes).hexdigest() not in (
                            status.get('scope_input_sha256'), status['artifact_sha256'].get('scope_results.json')):
                        raise ValueError('已有任务已结束，范围判断与已提交结果不同，请核对后显式重建')
                    return status
        source_format = detect_source_format(source)
        before = source.stat()
        scan = _SourceScan(matcher, source_format, case_sensitive)
        scan.read(source)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('扫描期间源码发生变化，请重新执行')
        if capture_format == 'auto':
            if source_format in {'omron_iec61131_10', 'smc2'}:
                capture_format = 'sysmac'
            elif any(vendor in scan.header for vendor in ('inovance', 'inoproshop', 'codesys')):
                capture_format = 'inovance'
            else:
                raise ValueError('源码头部未识别采集平台，请指定 --format inovance 或 --format sysmac')
        if default_type and capture_format != 'sysmac':
            raise ValueError('--default-type 仅用于 Sysmac 采集配置')
        if not automatic and not scan.references:
            raise ValueError(f'未找到可采集的显式变量。符号或表达式下标 {len(scan.dynamic)} 个，请核对匹配规则')
        channels, unresolved = {}, {}
        inventory, discovery_status = None, None
        if automatic:
            from plc_trace.capture.step_discovery import Discovery, array_coverage, replay_selectors, scope_index, scope_selection
            if source_format == 'plcopen':
                from plc_trace.capture.plcopen_discovery import discover
            else:
                from plc_trace.capture.sysmac_discovery import discover
            if scan.references:
                discovered = discover(source, scan)
            else:
                discovered = Discovery()
                discovered.issues['reason'] = 'no_static_executable_references'
            discovered.add_unproven_siblings(scan)
            inventory = discovered.inventory(digest, source_format)
            if pending and (pending.get('inventory_sha256') != inventory['inventory_sha256']
                            or pending.get('capture_format') != capture_format):
                raise ValueError('重算的候选证据或采集格式与待复核目录不同，请重新准备')
            decisions = json.loads(scope_bytes) if scope_bytes is not None else None
            if scope_results is not None and decisions is None:
                raise ValueError('用途判断 JSON 为空')
            chosen, roles = scope_selection(inventory, decisions)
            inventory['decisions'] = roles
            families = decisions.get('station_families', []) if decisions is not None else []
            inventory['station_families'] = families
            inventory['reference_sites'] = discovered.reference_sites
            slots, arrays = array_coverage(inventory, chosen, roles, discovered.reference_sites)
            inventory['step_arrays'] = arrays
            inventory['selector_replay'] = replay_selectors([r['name'] for r in (*chosen.values(), *slots.values())],
                                                           discovered.visible | set(discovered.candidates),
                                                           (n for n, _, _ in scan.references.values()))
            eligible = sum(r['eligibility'] == 'scope_required' for r in inventory['candidates'])
            single_writes = sum(r['eligibility'] == 'single_write' for r in inventory['candidates'])
            if decisions is None and (eligible or single_writes):
                discovery_status = 'scope_review_pending'
            for key, record in chosen.items():
                row = discovered.candidates[key]
                name = row.name
                channels[key] = {'name': name, 'source_line': record['transitions'][0]['source_line']}
                if capture_format == 'sysmac':
                    channels[key].update(type=row.declared, type_source='声明' if row.declared else '')
                if row.type_issue:
                    unresolved[name] = row.type_issue
            for key, slot in slots.items():
                channels[key] = {**slot, 'type_source': '同一实例数组的元素声明'}
            if not channels and not discovery_status:
                discovery_status = 'needs_review'
        else:
            for (_, unit), (source_name, raw, line) in scan.references.items():
                if capture_format == 'sysmac':
                    name, kind, reason, can_default = scan.sysmac_channel(source_name, unit)
                    if not any(matcher.fullmatch(value) for value in (name, source_name, raw)):
                        continue
                    origin = '声明'
                    if not kind and can_default and default_type:
                        kind, origin = default_type, '用户指定'
                    item = {'name': name, 'type': kind, 'type_source': origin if kind else '', 'source_line': line}
                    if not kind:
                        unresolved[name] = reason
                else:
                    name = source_name
                    if not (matcher.fullmatch(name) or matcher.fullmatch(raw)):
                        continue
                    item = {'name': name, 'source_line': line}
                channels.setdefault(name.casefold(), item)
            if not channels:
                raise ValueError('没有可按指定名称导出的采集变量，请核对变量作用域')
        ordered = sorted(channels.values(), key=lambda channel: _sort_key(channel['name']))
        if capture_format == 'sysmac':
            if every_n_cycles is not None:
                raise ValueError('--every-n-cycles 适用于汇川 .trace')
            task = {'task_name': validated_task_name(task_name) if task_name is not None else 'BaseControl',
                    'task_name_source': '用户指定' if task_name is not None else '模板默认'}
        else:
            template = ET.parse(RESOURCE_ROOT / 'templates' / 'inovance_trace.xml').getroot()
            task = sampling_task(scan.tasks, scan.program_writes, channels, template.find('./TraceConfiguration'),
                                 task_name=task_name, every_n_cycles=every_n_cycles)
        config = None
        files = {'static_variables.txt': ''.join(c['name'] + '\n' for c in ordered).encode('utf-8')}
        configurable = ordered
        if unresolved:
            configurable = ([c for c in ordered if c['name'] not in unresolved]
                            if automatic and capture_format == 'sysmac' else [])
            if automatic:
                files['unresolved_types.csv'] = _csv_bytes(('变量', '类型问题'), sorted(unresolved.items()))
        if configurable and not discovery_status and task['task_name'] and not scan.parse_issues:
            extension = 'csv' if capture_format == 'sysmac' else 'trace'
            suffix = '_partial' if len(configurable) < len(ordered) else ''
            config = f'{source.stem}_trace_config{suffix}.{extension}'
            files[config] = (_sysmac_config(configurable, task['task_name']) if capture_format == 'sysmac'
                             else _inovance_config(template, configurable, task, digest))
        report = {
            'version': __version__, 'mode': 'trace_setup', 'status': discovery_status or ('done' if config else 'needs_review'),
            'source': str(source), 'source_sha256': digest, 'source_format': source_format,
            'pattern': matcher.single_pattern if matcher else None,
            'patterns': matcher.patterns if matcher else [], 'capture_format': capture_format, **task,
            'static_variables': len(ordered), 'channels': ordered,
            'configured_variables': len(configurable) if config else 0,
            'configuration_complete': bool(config) and len(configurable) == len(ordered),
            'dynamic_variables': sorted(scan.dynamic.values(), key=_sort_key),
            'unresolved_types': unresolved, 'parse_issues': scan.parse_issues,
            'sample_count': 0, 'out': str(out), 'configuration': str(out / config) if config else None,
        }
        if automatic:
            report.update(selection_mode='automatic', discovery='step_variable_discovery.json',
                          sequence_variables=len(chosen), referenced_step_slots=len(slots),
                          scope_candidates=eligible, single_write_candidates=single_writes, inventory_sha256=inventory['inventory_sha256'],
                          scope_policy=inventory['scope_policy'], station_family_count=len(families),
                          scope_excluded=sum(r['admission'] == 'excluded' for r in roles),
                          scope_uncertain=sum(r['admission'] == 'review' for r in roles))
            resolved = {d['name'].casefold() for d in roles if d['admission'] != 'review'}
            resolved_singles = sum(r['eligibility'] == 'single_write' and r['identity'] in resolved
                                   for r in inventory['candidates'])
            report['unproven_candidates'] = len(inventory['candidates']) - eligible - resolved_singles
            files['step_array_coverage.csv'] = _csv_bytes(
                ('具体数组', '声明下界', '声明上界', '声明行', '执行步实例', '有引用无独立跳步的槽位'),
                ((g['array'], *g['bounds'], g['declaration_line'], g['example'], ' | '.join(g['referenced_slots'])) for g in arrays))
            if (unresolved or report['scope_uncertain'] or report['unproven_candidates']) and config:
                report['status'] = 'done_with_review'
            files['capture_context.csv'] = _csv_bytes(('步序变量', '所属例程', '外层执行条件', 'XML行', 'ST行', '写点引用'),
                                                       _capture_context(chosen))
            files['scope_candidates.jsonl'] = ''.join(json.dumps(r, ensure_ascii=False) + '\n'
                                                       for r in scope_index(inventory)).encode('utf-8')
            if decisions is not None:
                files['scope_results.json'] = json.dumps({'inventory_sha256': inventory['inventory_sha256'],
                    'station_families': families, 'decisions': roles}, ensure_ascii=False, indent=1).encode('utf-8')
            files['step_variable_discovery.json'] = json.dumps(inventory, ensure_ascii=False, indent=1).encode('utf-8')
        if fingerprint(source) != digest:
            raise ValueError('发现期间源码发生变化，请重新执行')
        report['request_sha256'] = request
        report['artifact_sha256'] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
        if scope_bytes is not None:
            report['scope_input_sha256'] = hashlib.sha256(scope_bytes).hexdigest()
        files['workflow_status.json'] = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8')
        publish(out, files, force=force, completing=pending is not None)
        return report


def add_task_arguments(parser):
    parser.add_argument('--task-name', help='指定采样任务，汇川按导出的源码任务表核对')
    parser.add_argument('--every-n-cycles', type=int, help='汇川每 N 个任务周期采样一次，默认 1')


def add_arguments(parser):
    parser.add_argument('--source', required=True, help='PLCopen、Sysmac IEC61131-10 或 SMC2 XML')
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument('--pattern', action='append',
                          help='完整变量名、通配符或 re: 正则；可重复指定，取并集并去重')
    selector.add_argument('--regex', help='对完整变量名匹配的 Python 正则')
    parser.add_argument('--scope-results', help='自动发现后由 Agent 按本轮源码证据生成的用途判断 JSON')
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--out', help='实际运行目录，合并时使用首轮返回的 out')
    destination.add_argument('-output', dest='output_root', help='输出根目录，在其中选择本任务的工序子目录')
    parser.add_argument('--format', choices=('auto', 'inovance', 'sysmac'), default='auto')
    add_task_arguments(parser)
    parser.add_argument('--default-type', type=str.upper, choices=sorted(INTEGER_TYPES),
                        help='Sysmac 缺少根变量声明时使用的已核实整数类型')
    parser.add_argument('--case-sensitive', action='store_true', help='按原大小写匹配变量')
    parser.add_argument('--force', action='store_true', help='归档旧采集配置后重建')


def run(args):
    pattern = args.pattern if args.regex is None else 're:' + args.regex
    out = args.out
    if args.output_root is not None:
        from plc_trace.output.utils import infer_proc_name
        out = Path(args.output_root) / (infer_proc_name(args.source) + '_trace_setup')
    try:
        report = generate(args.source, out, pattern, capture_format=args.format,
                          task_name=args.task_name,
                          every_n_cycles=args.every_n_cycles, default_type=args.default_type,
                          case_sensitive=args.case_sensitive, force=args.force, scope_results=args.scope_results,
                          choose_available=args.output_root is not None)
    except (OSError, ValueError, re.error, etree.XMLSyntaxError) as exc:
        raise SystemExit(f'采集配置生成失败：{exc}') from exc
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    run(parser.parse_args())
