"""Find explicit PLC references and prepare channel configurations for a new capture."""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5
from xml.etree import ElementTree as ET

from lxml import etree

from plc_trace import __version__
from plc_trace.paths import PROJECT_ROOT, RESOURCE_ROOT

from plc_trace.analysis.boundary_logic import integer, symbol
from plc_trace.analysis.candidate_filter import glob_regex
from plc_trace.source.smc2_source import iter_networks
from plc_trace.source.source_format import detect_source_format
from plc_trace.trace.trace_to_pairs import INTEGER_TYPES
from plc_trace.runtime.workflow import _fresh_output, fingerprint, lock_run_dir


_NAME = r'[^\W\d]\w*'
_ROOT = re.compile(rf'(?<![\w.#]){_NAME}')
_MEMBER_NAME = re.compile(rf'{_NAME}|\d+')
_SUFFIX = re.compile(rf'\s*(?:\.\s*(?:{_NAME}|\d+)|(?P<index>\[))')
_CONTINUATION = re.compile(r'\s*(?:\[|\.(?!\.)|\()')
_INDEX_EDGE = re.compile(r'[\[\];]')
_SUBSCRIPT = re.compile(r'\[([^\[\]]+)\]')
_ARRAY_TYPE = re.compile(r'ARRAY\s*\[([^\[\]]+)\]\s*OF\s+(.+)', re.I)
_IGNORED = re.compile(
    r'''//[^\r\n]*|\(\*|"(?:\$[\s\S]|""|[^"])*(?:"|\Z)|'(?:\$[\s\S]|''|[^'])*(?:'|\Z)'''
)
_COMMENT_EDGE = re.compile(r'\(\*|\*\)')
_XML_COMMENTS = {'comment', 'comments', 'documentation', 'networkcomment', 'description'}
_EXPRESSION_ATTRIBUTES = {'identifier', 'expression', 'variable', 'var', 'reference'}


def _executable_text(text):
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


def _channels(text):
    for part in _executable_text(text):
        position = 0
        while match := _ROOT.search(part, position):
            position, complete = _reference_end(part, match.end())
            if not complete:
                continue
            name = symbol(part[match.start():position])
            indexes = _SUBSCRIPT.findall(name)
            # An inner bracket denotes an expression, even when its own index is constant.
            if name.count('[') != len(indexes):
                yield name, name, True
                continue
            values = [tuple(_constant(value) for value in index.split(',')) for index in indexes]
            if any(None in index for index in values):
                yield name, name, True
                continue
            groups = iter(values)
            canonical = _SUBSCRIPT.sub(lambda _: '[' + ','.join(map(str, next(groups))) + ']', name)
            yield name, canonical, False


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


class _SourceScan:
    def __init__(self, matcher, source_format, case_sensitive):
        self.matcher = matcher
        self.source_format = source_format
        self.case_sensitive = case_sensitive
        self.references = {}
        self.dynamic = {}
        self.declarations = defaultdict(set)
        self.members = defaultdict(set)
        self.aliases = defaultdict(set)
        self.programs = {}
        self.header = ''
        self.parse_issues = []

    def _read(self, text, unit, line):
        if not text or not text.strip():
            return
        for raw, name, dynamic in _channels(text):
            names = (name, raw)
            if unit and unit[0] == 'program':
                names += (f'{unit[1]}.{name}', f'{unit[1]}.{raw}')
            if any(self.matcher.fullmatch(value) for value in names):
                key = name if self.case_sensitive else name.casefold()
                if dynamic:
                    self.dynamic.setdefault(key, name)
                else:
                    self.references.setdefault((key, unit), (name, raw, line))

    def read(self, source):
        if self.source_format == 'smc2':
            audit = {}
            for record in iter_networks(source, audit):
                for value in _smc_values(record['net']['CLs']):
                    self._read(value, None, record['source_line'])
            self.parse_issues = audit.get('parse_issues', [])
            return
        frames = [_Frame('', None, False, False, '')]
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
                    if tag == 'fileheader':
                        self.header = ' '.join(node.get(k, '') for k in ('companyName', 'productName')).casefold()
                    if tag == 'program' and name:
                        self.programs[name.casefold()] = unit
                    continue
                frame = frames.pop()
                parent = frames[-1]
                if frame.body and not frame.comment:
                    self._read(node.text, frame.unit, node.sourceline)
                    for attribute, value in node.attrib.items():
                        if _local_name(attribute) in _EXPRESSION_ATTRIBUTES:
                            self._read(value, frame.unit, node.sourceline)
                if parent.body and not parent.comment:
                    self._read(node.tail, parent.unit, node.sourceline)
                if frame.tag == 'typename' and self.source_format == 'omron_iec61131_10':
                    self._type_name(node, frame)
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

    def _peel_type(self, values, part):
        """Consume exactly the array dimensions addressed by one member."""
        if len(values) != 1:
            return None, '声明缺失或冲突'
        value, = values
        seen = set()
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
                if lower is not None and upper is not None and not lower <= int(index) <= upper:
                    return None, '下标超出声明范围'
            pending = next(indexes, None)

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
            kind, problem = self._peel_type(self.members.get((kind.casefold(), member), ()), part)
        if kind in INTEGER_TYPES:
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


def _inovance_config(channels, task, source_digest):
    template = RESOURCE_ROOT / 'templates' / 'inovance_trace.xml'
    root = ET.parse(template).getroot()
    record = root.find(".//Single[@Name='Record']")
    record.find("./Single[@Name='TaskName']").text = task
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


def _publish(out, files, force):
    # Stage all writes before moving an existing run out of the way.
    with TemporaryDirectory(prefix='.trace-setup-', dir=out.parent) as temporary:
        stage = Path(temporary) / 'ready'
        stage.mkdir()
        for name, contents in files.items():
            (stage / name).write_bytes(contents)
        previous = _fresh_output(out, force)
        try:
            out.rmdir()
            stage.rename(out)
        except OSError:
            if previous and not out.exists():
                Path(previous).rename(out)
            raise


def generate(source, out, pattern, *, capture_format='auto', task_name=None, default_type=None,
             case_sensitive=False, force=False):
    source_input = Path(source).absolute()
    source, out = source_input.resolve(), Path(out).resolve()
    if (source_input.is_relative_to(out) or source.is_relative_to(out) or Path.cwd().is_relative_to(out)
            or PROJECT_ROOT.is_relative_to(out)):
        raise ValueError('采集配置输出须使用独立目录，与源码和程序分开')
    if not pattern or not pattern.strip():
        raise ValueError('匹配规则不能为空')
    if capture_format not in ('auto', 'sysmac', 'inovance'):
        raise ValueError('采集格式须为 auto、sysmac 或 inovance')
    if default_type and default_type not in INTEGER_TYPES:
        raise ValueError('默认类型须为采集分析支持的整数类型')
    matcher = glob_regex(pattern.strip())
    if matcher.fullmatch(''):
        raise ValueError('匹配规则可以匹配空名称，请限定所需的步序变量')
    if case_sensitive:
        matcher = re.compile(matcher.pattern, matcher.flags & ~re.IGNORECASE)
    with lock_run_dir(out):
        if out.exists() and not out.is_dir():
            raise ValueError('输出路径须为目录')
        if out.exists() and any(out.iterdir()):
            if not force:
                raise ValueError('请使用空输出目录，或以 --force 归档已有的采集配置')
            marker = out / 'workflow_status.json'
            status = json.loads(marker.read_bytes()) if marker.is_file() else None
            if not isinstance(status, dict) or status.get('mode') != 'trace_setup':
                raise ValueError('输出目录属于其他任务，请使用空目录')
        source_format = detect_source_format(source)
        before = source.stat()
        digest = fingerprint(source)
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
        if not scan.references:
            raise ValueError(f'未找到可采集的显式变量。符号或表达式下标 {len(scan.dynamic)} 个，请核对匹配规则')
        channels, unresolved = {}, {}
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
        task = task_name if task_name is not None else ('BaseControl' if capture_format == 'sysmac' else 'MainTask')
        if not task.strip() or any(ch in task for ch in '\r\n'):
            raise ValueError('采集任务名称须为非空单行')
        config = None
        files = {'static_variables.txt': ''.join(c['name'] + '\n' for c in ordered).encode('utf-8')}
        if not unresolved and not scan.parse_issues:
            extension = 'csv' if capture_format == 'sysmac' else 'trace'
            config = source.stem + '_trace_config.' + extension
            files[config] = (_sysmac_config(ordered, task) if capture_format == 'sysmac'
                             else _inovance_config(ordered, task, digest))
        report = {
            'version': __version__, 'mode': 'trace_setup', 'status': 'done' if config else 'needs_review',
            'source': str(source), 'source_sha256': digest, 'source_format': source_format,
            'pattern': pattern, 'capture_format': capture_format, 'task_name': task,
            'task_name_source': '用户指定' if task_name else '模板默认',
            'static_variables': len(ordered), 'channels': ordered,
            'dynamic_variables': sorted(scan.dynamic.values(), key=_sort_key),
            'unresolved_types': unresolved, 'parse_issues': scan.parse_issues,
            'sample_count': 0, 'out': str(out), 'configuration': str(out / config) if config else None,
        }
        files['workflow_status.json'] = json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8')
        _publish(out, files, force)
        return report


def add_arguments(parser):
    parser.add_argument('--source', required=True, help='PLCopen、Sysmac IEC61131-10 或 SMC2 XML')
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument('--pattern', help='完整变量名、通配符或 re: 正则')
    selector.add_argument('--regex', help='对完整变量名匹配的 Python 正则')
    parser.add_argument('--out', required=True, help='独立输出目录')
    parser.add_argument('--format', choices=('auto', 'inovance', 'sysmac'), default='auto')
    parser.add_argument('--task-name', help='在采集客户端使用的 PLC 任务名')
    parser.add_argument('--default-type', type=str.upper, choices=sorted(INTEGER_TYPES),
                        help='Sysmac 缺少根变量声明时使用的已核实整数类型')
    parser.add_argument('--case-sensitive', action='store_true', help='按原大小写匹配变量')
    parser.add_argument('--force', action='store_true', help='归档旧采集配置后重建')


def run(args):
    pattern = args.pattern if args.regex is None else 're:' + args.regex
    try:
        report = generate(args.source, args.out, pattern, capture_format=args.format,
                          task_name=args.task_name, default_type=args.default_type,
                          case_sensitive=args.case_sensitive, force=args.force)
    except (OSError, ValueError, re.error, etree.XMLSyntaxError) as exc:
        raise SystemExit(f'采集配置生成失败：{exc}') from exc
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    run(parser.parse_args())
