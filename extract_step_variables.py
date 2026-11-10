#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Step 1 only: deterministic, source-wide PLC variable-name extraction.
Python 3.10+, standard library only; no LLM, API, GT or PLC execution.
Example:
    python extract_step_variables.py --source source.xml \
        --pattern 'EM*.Process_steps[*]' --out outputs/run1
Default scope is *all source text*, including comments and string literals.
Only explicit names are returned; array declarations and aliases are not expanded.
"""
from __future__ import annotations
import argparse
import codecs
import csv
import hashlib
import io
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from array import array
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
from typing import Iterator, Pattern
VERSION = "1.3.0"
VARIABLE_LIST = "static_variables.txt"
# 固定的命名空间UUID，用于 uuid5 生成确定性GUID（变量名 -> GUID）
_TRACE_GUID_NAMESPACE = uuid.UUID("a3b7c8d9-e1f2-4a3b-9c5d-7e8f90a1b2c3")
# 曲线颜色循环池（CODESYS trace 常见颜色，BGR int）
_TRACE_COLORS = (-23296, -16711936, -16776961, -7876885, -65536, -16711681,
                 -256, -8355712, -16776960, -657956)
# Record 级固定 GUID（确定性生成，整文件不变）
_TRACE_GUID_POU = str(uuid.uuid5(_TRACE_GUID_NAMESPACE, "Record.POU"))
_TRACE_APP_GUID = str(uuid.uuid5(_TRACE_GUID_NAMESPACE, "App"))
_TRACE_DOWNLOAD_GUID = str(uuid.uuid5(_TRACE_GUID_NAMESPACE, "Download"))
_TRACE_EXIT_GUID = str(uuid.uuid5(_TRACE_GUID_NAMESPACE, "Exit"))
# A source reference is a complete identifier path, optionally indexed. This
# includes Chinese identifiers and dotted/numeric members; matching a suffix of
# SomeObject.EM01V.Process_steps[0] is deliberately NOT allowed.
_IDENTIFIER = r"[^\W\d]\w*"
_ARRAY = r"(?:[ \t]*\[[^\[\]\r\n<>=;\x27\x22]+\])*"
_REFERENCE = re.compile(
    r"(?<![\w.])" + _IDENTIFIER + _ARRAY
    + r"(?:[ \t]*\.[ \t]*(?:" + _IDENTIFIER + r"|\d+)" + _ARRAY + r")*"
)
_TYPED_INTEGER = re.compile(
    r"(?:(?:SINT|USINT|INT|UINT|DINT|UDINT|LINT|ULINT)#)?[+-]?[0-9]+", re.I
)
_BRACKET_CONTENT = re.compile(r"\[([^\[\]]+)\]")
class ExtractionError(ValueError):
    """An actionable input/output or matching error."""
def normalize_reference(value: str) -> str:
    """Normalize syntax whitespace, but preserve spaces inside word operators."""
    value = value.strip()
    if " " not in value and "\t" not in value:
        return value
    value = re.sub(r"[ \t]*([.\[\],])[ \t]*", r"\1", value)
    value = re.sub(r"[ \t]*([+*/-])[ \t]*", r"\1", value)
    return re.sub(r"[ \t]+", " ", value)
def compile_name_pattern(pattern: str, mode: str = "glob", case_sensitive: bool = False) -> Pattern[str]:
    """Compile a full-name matcher. Glob brackets are LITERAL, unlike fnmatch.
    Glob: * = zero or more characters; ? = one character; every other character
    is literal. The matcher only sees an already extracted variable reference.
    Regex: standard Python regex, applied by fullmatch to that full reference.
    """
    if not pattern or not pattern.strip():
        raise ExtractionError("匹配规则不能为空。")
    if mode == "glob":
        value = normalize_reference(pattern)
        expression = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in value)
    elif mode == "regex":
        expression = pattern
    else:
        raise ExtractionError("mode 必须是 glob 或 regex。")
    try:
        matcher = re.compile(expression, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise ExtractionError(f"正则表达式无效：{exc}") from exc
    if matcher.fullmatch(""):
        raise ExtractionError("规则可以匹配空名称，请改成更具体的变量名规则。")
    return matcher
def iter_line_matches(line: str, matcher: Pattern[str]) -> Iterator[tuple[str, str, int, int]]:
    """Yield (normalized name, original spelling, start, end) for one source line.
    Position is in Unicode characters; start is zero-based and end is exclusive.
    References split across physical lines are intentionally not reconstructed.
    """
    for match in _REFERENCE.finditer(line):
        raw = match.group(0)
        name = normalize_reference(raw)
        if matcher.fullmatch(name):
            # Reject a partial token where unsupported nested indexing or member
            # syntax follows, rather than silently reporting the shorter prefix.
            immediate = line[match.end():match.end() + 1]
            if immediate and (immediate.isalnum() or immediate == "_"):
                continue
            tail = line[match.end():].lstrip(" \t")
            if tail.startswith(("[", ".")):
                continue
            yield name, raw, match.start(), match.end()
# One layer of representation decoding, never repeated "until nothing changes".
# A Unicode position map keeps occurrence coordinates in the ORIGINAL file.
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\\r\n])*"')
_JSON_ESCAPE = re.compile(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])')
_XML_ENTITY = re.compile(r'&(?:#[0-9]+|#x[0-9a-fA-F]+|amp|lt|gt|quot|apos);')
_XML_ENTITIES = {'&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'"}
@dataclass
class DecodedLine:
    text: str
    starts: array | None = None
    ends: array | None = None
    transformations: tuple[str, ...] = ()
    def original_span(self, start: int, end: int) -> tuple[int, int]:
        if self.starts is None:
            return start, end
        return self.starts[start], self.ends[end - 1]
def _apply_edits(view: DecodedLine, edits: list[tuple[int, int, str]], kind: str) -> DecodedLine:
    """Apply nonoverlapping edits, retaining per-character original coordinates."""
    if not edits:
        return view
    pieces: list[str] = []
    starts, ends = array('Q'), array('Q')
    pos = 0
    def copy_span(a: int, b: int) -> None:
        pieces.append(view.text[a:b])
        if view.starts is None:
            starts.extend(range(a, b))
            ends.extend(range(a + 1, b + 1))
        else:
            starts.extend(view.starts[a:b])
            ends.extend(view.ends[a:b])
    for a, b, replacement in edits:
        if a < pos or b <= a:
            raise ExtractionError('内部解码位置重叠；未发布结果。')
        copy_span(pos, a)
        original_start, original_end = view.original_span(a, b)
        pieces.append(replacement)
        starts.extend(array('Q', [original_start]) * len(replacement))
        ends.extend(array('Q', [original_end]) * len(replacement))
        pos = b
    copy_span(pos, len(view.text))
    return DecodedLine(''.join(pieces), starts, ends, view.transformations + (kind,))
def _xml_char(entity: str) -> str:
    if entity in _XML_ENTITIES:
        return _XML_ENTITIES[entity]
    n = int(entity[3:-1], 16) if entity.startswith('&#x') else int(entity[2:-1])
    # XML 1.0 characters only. Invalid entities remain visible as original text.
    if n in (9, 10, 13) or 0x20 <= n <= 0xD7FF or 0xE000 <= n <= 0xFFFD or 0x10000 <= n <= 0x10FFFF:
        return chr(n)
    return entity
class SourceDecoder:
    """Decode XML entities outside CDATA and JSON string escapes on JSON-shaped lines.
    This is a representation reader, NOT a complete XML/JSON/PLC parser.
    JSON strings must fit on one physical line. Strings/comments remain in scope.
    """
    def __init__(self, mode: str = 'auto', xml_source: bool = False):
        if mode not in ('auto', 'none'):
            raise ExtractionError('--decode-escapes 只能是 auto 或 none。')
        self.mode = mode
        self.xml_source = xml_source
        self.in_cdata = False
        self.stats = {'xml_entities_decoded': 0, 'json_strings_decoded': 0,
                      'json_escapes_decoded': 0, 'lines_decoded': 0,
                      'invalid_json_strings_left_raw': 0,
                      'unpaired_surrogates_left_raw': 0}
    def decode(self, raw: str) -> DecodedLine:
        view = DecodedLine(raw)
        if self.mode == 'none':
            return view
        edits: list[tuple[int, int, str]] = []
        if self.xml_source:
            # XML entities have no representation semantics INSIDE CDATA.
            pos = 0
            while pos < len(raw):
                if self.in_cdata:
                    stop = raw.find(']]>', pos)
                    if stop < 0:
                        break
                    pos = stop + 3
                    self.in_cdata = False
                else:
                    stop = raw.find('<![CDATA[', pos)
                    end = len(raw) if stop < 0 else stop
                    if '&' in raw[pos:end]:
                        for m in _XML_ENTITY.finditer(raw, pos, end):
                            value = _xml_char(m.group())
                            if value != m.group():
                                edits.append((m.start(), m.end(), value))
                    if stop < 0:
                        break
                    self.in_cdata = True
                    pos = stop + 9
            self.stats['xml_entities_decoded'] += len(edits)
            view = _apply_edits(view, edits, 'xml_entity')
        text = view.text
        # Reject arbitrary ST/Windows-path text; decode only JSON-shaped lines.
        probe = text.lstrip()
        if '<![CDATA[' in probe:
            probe = probe.split('<![CDATA[', 1)[1].lstrip()
        looks_json = probe.startswith(('{', '[')) or bool(re.match(r'^"(?:\\.|[^"\\])*"\s*:', probe))
        edits = []
        if looks_json and '\\' in text:
            for token in _JSON_STRING.finditer(text):
                spelling = token.group()
                if '\\' not in spelling:
                    continue
                try:
                    json.loads(spelling)  # Validate token before interpreting escapes.
                except (ValueError, TypeError):
                    self.stats['invalid_json_strings_left_raw'] += 1
                    continue
                changes = 0
                escapes = list(_JSON_ESCAPE.finditer(spelling))
                skip = -1
                for e in escapes:
                    if e.start() < skip:
                        continue
                    value = json.loads('"' + e.group() + '"')
                    end = e.end()
                    if len(value) == 1 and 0xD800 <= ord(value) <= 0xDBFF:
                        following = re.match(r'\\u([0-9a-fA-F]{4})', spelling[end:])
                        if following and 0xDC00 <= int(following.group(1), 16) <= 0xDFFF:
                            value = chr(0x10000 + ((ord(value) - 0xD800) << 10) + int(following.group(1), 16) - 0xDC00)
                            end += len(following.group())
                            skip = end
                        else:
                            self.stats['unpaired_surrogates_left_raw'] += 1
                            continue
                    elif len(value) == 1 and 0xDC00 <= ord(value) <= 0xDFFF:
                        self.stats['unpaired_surrogates_left_raw'] += 1
                        continue
                    edits.append((token.start() + e.start(), token.start() + end, value))
                    changes += 1
                if changes:
                    self.stats['json_strings_decoded'] += 1
                    self.stats['json_escapes_decoded'] += changes
            view = _apply_edits(view, edits, 'json_escape')
        if view.transformations:
            self.stats['lines_decoded'] += 1
        return view
    def iter_matches(self, raw: str, matcher: Pattern[str]):
        view = self.decode(raw)
        # Never scan raw AND decoded text together: each source occurrence counts once.
        for name, decoded_raw, start, end in iter_line_matches(view.text, matcher):
            a, b = view.original_span(start, end)
            yield name, raw[a:b], a, b, decoded_raw, '+'.join(view.transformations) or 'raw'
def natural_key(value: str) -> tuple:
    """Sort 2 before 10, with a stable lexical tie-break for equal numeric parts."""
    parts = tuple((0, int(p)) if p.isdigit() else (1, p.casefold()) for p in re.split(r"(\d+)", value))
    return parts, value.casefold(), value
def index_kind(name: str) -> str:
    indexes = _BRACKET_CONTENT.findall(name)
    if not indexes:
        return "无数组下标"
    if all(all(_TYPED_INTEGER.fullmatch(v.strip()) for v in index.split(",")) for index in indexes):
        return "常量下标"
    return "符号或表达式下标（未求值）"
def choose_encoding(source: Path, requested: str = "auto") -> str:
    if requested != "auto":
        try:
            codecs.lookup(requested)
        except LookupError as exc:
            raise ExtractionError(f"未知编码：{requested}") from exc
        return requested
    with source.open("rb") as stream:
        head = stream.read(512)
    # UTF-32 must be checked before UTF-16, whose BOM is a prefix.
    for mark, name in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                       (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"),
                       (codecs.BOM_UTF8, "utf-8-sig")):
        if head.startswith(mark):
            return name
    declaration = re.search(br'<\?xml\b[^>]*\bencoding\s*=\s*[\x27\x22]([^\x27\x22]+)', head, re.I)
    if declaration:
        try:
            name = declaration.group(1).decode("ascii")
            codecs.lookup(name)
            return name
        except (UnicodeError, LookupError) as exc:
            raise ExtractionError("XML中的编码声明无效，请用 --encoding 明确指定。") from exc
    return "utf-8-sig"
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
@dataclass
class VariableRecord:
    name: str
    count: int
    first_line: int
    first_column: int
    last_line: int
    spellings: set[str] = field(default_factory=set)
def _var_guid(name: str) -> str:
    return str(uuid.uuid5(_TRACE_GUID_NAMESPACE, "var:" + name))
def _axis_settings_yaml(min_val: float = -12, max_val: float = 12,
                        tick_dist: float = 1) -> str:
    return (
        '            <Single Name="Mode" Type="int">0</Single>\n'
        f'            <Single Name="Min" Type="double">{min_val}</Single>\n'
        f'            <Single Name="Max" Type="double">{max_val}</Single>\n'
        f'            <Single Name="Range" Type="double">{max_val - min_val}</Single>\n'
        '            <Single Name="DrawDesc" Type="bool">False</Single>\n'
        '            <Single Name="Desc" Type="string"></Single>\n'
        '            <Single Name="Col" Type="int">-16777216</Single>\n'
        '            <Single Name="Font" Type="{3371a5fa-e13c-4960-9e27-0a2c6ba51cd5}" Method="IArchivable">\n'
        '              <Single Name="Name" Type="string">Arial</Single>\n'
        '              <Single Name="Size" Type="float">8</Single>\n'
        '              <Single Name="Flags" Type="uint">0</Single>\n'
        '            </Single>\n'
        '            <Single Name="Grid" Type="bool">True</Single>\n'
        '            <Single Name="GridCol" Type="int">-8355712</Single>\n'
        '            <Single Name="TickMarkFixedSpacing" Type="bool">False</Single>\n'
        f'            <Single Name="TickMarkFixedDistance" Type="double">{tick_dist}</Single>\n'
        '            <Single Name="TickMarkFixedSubdivisions" Type="int">1</Single>\n'
        '          </Single>'
    )
def _build_variable_list_entry(name: str, color: int) -> str:
    guid = _var_guid(name)
    return (
        '        <Single Type="{b6a18d24-a045-4a81-a2ac-7044c6f553c0}" Method="IArchivable">\n'
        f'          <Single Name="VariableName" Type="string">{name}</Single>\n'
        '          <Dictionary Type="System.Collections.Hashtable" Name="PlotLib" />\n'
        f'          <Single Name="GraphColor" Type="int">{color}</Single>\n'
        '          <Single Name="GraphName" Type="string"></Single>\n'
        '          <Single Name="GraphType" Type="int">1</Single>\n'
        '          <Single Name="Visible" Type="bool">True</Single>\n'
        '          <Single Name="MaxWarningArea" Type="float">0</Single>\n'
        '          <Single Name="ActivateMaxWarning" Type="bool">False</Single>\n'
        '          <Single Name="MaxColor" Type="int">-65536</Single>\n'
        '          <Single Name="MinWarningArea" Type="float">0</Single>\n'
        '          <Single Name="ActivateMinWarning" Type="bool">False</Single>\n'
        '          <Single Name="MinColor" Type="int">-16777216</Single>\n'
        '          <Single Name="YAxis" Type="int">0</Single>\n'
        f'          <Single Name="guid" Type="System.Guid">{guid}</Single>\n'
        '          <Single Name="YAxisSettings" Type="{6ace1446-1a6b-4117-97c0-65c2081a8054}" Method="IArchivable">\n'
        + _axis_settings_yaml(-12, 12, 1) + '\n'
        '          <Single Name="Enabled" Type="bool">True</Single>\n'
        '        </Single>'
    )
def _build_diagram_entry(name: str, idx: int) -> str:
    guid = _var_guid(name)
    return (
        '        <Single Type="{528b663d-e04a-446a-80df-13e5dea35f57}" Method="IArchivable">\n'
        '          <Single Name="Appearance" Type="{0ebf0881-00e4-43f4-9e5d-c297518b153e}" Method="IArchivable">\n'
        '            <Single Name="DiagramSettings" Type="{61889f9a-966b-4cce-8523-bdfa9a5ab097}" Method="IArchivable">\n'
        '              <Single Name="Col" Type="int">-1</Single>\n'
        '              <Single Name="ColSel" Type="int">-657956</Single>\n'
        '            </Single>\n'
        '            <Single Name="YAxis" Type="{6ace1446-1a6b-4117-97c0-65c2081a8054}" Method="IArchivable">\n'
        + _axis_settings_yaml(-12, 12, 1) + '\n'
        '          </Single>\n'
        '          <Single Name="Variables" Type="{732a698e-182d-4c08-be90-b42c1a34ebae}" Method="IArchivable">\n'
        '            <List2 Name="Variables">\n'
        '              <Single Type="{8d42fb88-c20c-44ee-9563-111d64032744}" Method="IArchivable">\n'
        f'                <Single Name="ReferencedVarGuid" Type="System.Guid">{guid}</Single>\n'
        '              </Single>\n'
        '            </List2>\n'
        '          </Single>\n'
        f'          <Single Name="Name" Type="string">Diagram {idx}</Single>\n'
        '          <Single Name="Visible" Type="bool">True</Single>\n'
        '        </Single>'
    )
def _build_trace_variable_entry(name: str) -> str:
    return (
        f'      <TraceVariable VarName="{name}" VariableIndex="0" Type="System.Double">\n'
        '        <Values></Values>\n'
        '        <Timestamps></Timestamps>\n'
        '      </TraceVariable>'
    )
def build_trace_xml(variables: list[str]) -> str:
    if not variables:
        raise ExtractionError("没有固定下标变量，无法生成 .trace 文件。")
    var_entries = "\n".join(
        _build_variable_list_entry(n, _TRACE_COLORS[i % len(_TRACE_COLORS)])
        for i, n in enumerate(variables)
    )
    diagram_entries = "\n".join(
        _build_diagram_entry(n, i + 1) for i, n in enumerate(variables)
    )
    trace_var_entries = "\n".join(_build_trace_variable_entry(n) for n in variables)
    return (
        '<Trace>\n'
        '  <TraceConfiguration>\n'
        '    <Single xml:space="preserve" Type="{f7aa3620-8073-4c91-b6ec-86ed9eb60303}" Method="IArchivable">\n'
        '  <Dictionary Type="System.Collections.Hashtable" Name="TraceDataConfig" />\n'
        '  <Dictionary Type="System.Collections.Hashtable" Name="TraceOutputConfig" />\n'
        '  <Single Name="ConfigSource" Type="{479416f8-8a4a-44fd-9c85-4f240ddd35c5}">Project</Single>\n'
        '  <Single Name="PlcApplication" Type="string"></Single>\n'
        '  <Single Name="TraceSettings" Type="{8aaaee37-d2e1-4e99-b58f-acd4f8bf698f}" Method="IArchivable">\n'
        '    <Single Name="AutoSave" Type="bool">False</Single>\n'
        '    <Null Name="AutoSaveFileName" />\n'
        '    <Single Name="RefreshDuration" Type="int">500</Single>\n'
        '    <Single Name="BufferPerVariable" Type="uint">1000000</Single>\n'
        '    <Single Name="HorizontalOrder" Type="bool">True</Single>\n'
        '    <Single Name="OverrideRTSBufferSize" Type="bool">False</Single>\n'
        '  </Single>\n'
        '  <Null Name="RecordList" />\n'
        '  <Single Name="Record" Type="{988743cb-81f4-49a7-8a04-2490e2ab1c5e}" Method="IArchivable">\n'
        '    <Dictionary Type="System.Collections.Hashtable" Name="PlotLib" />\n'
        '    <Single Name="VariableList" Type="{adcdd50b-433c-4999-986a-b4c67e7333be}" Method="IArchivable">\n'
        '      <List Name="InnerList" Type="System.Collections.ArrayList">\n'
        + var_entries + '\n'
        '      </List>\n'
        '    </Single>\n'
        '    <Single Name="TriggerVariable" Type="string"></Single>\n'
        '    <Single Name="TriggerEdge" Type="{33a32e6c-d512-4195-9e73-520baca05512}">None</Single>\n'
        '    <Single Name="TriggerLevel" Type="string"></Single>\n'
        '    <Single Name="TriggerPosition" Type="byte">51</Single>\n'
        '    <Single Name="TriggerFlags" Type="{7be0d640-5118-4d1b-bbfd-95754f90c80d}">Undefined</Single>\n'
        '    <Single Name="TaskName" Type="string">MainTask</Single>\n'
        '    <Single Name="BufferEntries" Type="uint">501</Single>\n'
        '    <Single Name="EveryNCycles" Type="uint">1</Single>\n'
        f'    <Single Name="guidPOU" Type="System.Guid">{_TRACE_GUID_POU}</Single>\n'
        '    <Single Name="Comment" Type="string"></Single>\n'
        '    <Single Name="Condition" Type="string"></Single>\n'
        '    <Single Name="MicroSeconds" Type="bool">False</Single>\n'
        '    <Single Name="Autostart" Type="bool">False</Single>\n'
        '    <Single Name="GenerateVisuCode" Type="bool">False</Single>\n'
        '    <Single Name="Visible" Type="bool">True</Single>\n'
        '    <Single Name="Selected" Type="bool">False</Single>\n'
        '    <Single Name="RecordName" Type="string">StepNum</Single>\n'
        '    <Single Name="PostTriggerSamples" Type="uint">51</Single>\n'
        '    <Single Name="Appearance" Type="{99dff181-10ad-4b60-af8a-66ab1d5eebe9}" Method="IArchivable">\n'
        '      <Single Name="tAxis" Type="{6ace1446-1a6b-4117-97c0-65c2081a8054}" Method="IArchivable">\n'
        + _axis_settings_yaml(0, 10000, 1000).replace('            ', '        ') + '\n'
        '      <Single Name="yAxisCommon" Type="{6ace1446-1a6b-4117-97c0-65c2081a8054}" Method="IArchivable">\n'
        + _axis_settings_yaml(-12, 12, 1).replace('            ', '        ') + '\n'
        '      <Single Name="diagSettings" Type="{61889f9a-966b-4cce-8523-bdfa9a5ab097}" Method="IArchivable">\n'
        '        <Single Name="Col" Type="int">-1</Single>\n'
        '        <Single Name="ColSel" Type="int">-657956</Single>\n'
        '      </Single>\n'
        '    </Single>\n'
        '    <Single Name="TraceType" Type="{84128cb1-5f00-41f0-9b8a-4a7318fc5ddc}">Single</Single>\n'
        '    <Single Name="nSampingsPerFile" Type="int">0</Single>\n'
        '    <Single Name="nMaxDataSavingSize" Type="int">0</Single>\n'
        '    <Single Name="tMaxSamplingTime" Type="System.TimeSpan">1.00:00:00</Single>\n'
        '    <Single Name="eWhenTheUpperLimitReached" Type="{1978417f-c844-4e81-b546-f5722eec222b}">StopTracing</Single>\n'
        '    <Null Name="stSaveIn" />\n'
        '    <Null Name="stFilePrefix" />\n'
        '    <List Name="SamplingCSVFiles" Type="System.Collections.ArrayList" />\n'
        '    <Single Name="nFileIndex" Type="long">0</Single>\n'
        '    <Single Name="nFileIndexSize" Type="int">0</Single>\n'
        '    <Single Name="WrittenWithVersionGE_V3420" Type="bool">True</Single>\n'
        '    <Single Name="WrittenWithVersionGE_V3510" Type="bool">True</Single>\n'
        '    <Single Name="TimeAxis" Type="{6ace1446-1a6b-4117-97c0-65c2081a8054}" Method="IArchivable">\n'
        + _axis_settings_yaml(0, 10000, 1000).replace('            ', '      ') + '\n'
        '    <Single Name="Diagrams" Type="{09c4e4de-82a7-4bc3-b8ac-0eed481c722d}" Method="IArchivable">\n'
        '      <List2 Name="Diagrams">\n'
        + diagram_entries + '\n'
        '      </List2>\n'
        '    </Single>\n'
        '  </Single>\n'
        f'  <Single Name="AppGuid" Type="System.Guid">{_TRACE_APP_GUID}</Single>\n'
        f'  <Single Name="DownloadGuid" Type="System.Guid">{_TRACE_DOWNLOAD_GUID}</Single>\n'
        f'  <Single Name="ExitGuid" Type="System.Guid">{_TRACE_EXIT_GUID}</Single>\n'
        '  <Dictionary Type="System.Collections.Hashtable" Name="VisuCodeGuids" />\n'
        '</Single>\n'
        '  </TraceConfiguration>\n'
        '  <TraceData Version="1.0.0.0">\n'
        '    <TraceRecord>\n'
        '      <TriggerState>Disabled</TriggerState>\n'
        '      <StartTime>0</StartTime>\n'
        + trace_var_entries + '\n'
        '    </TraceRecord>\n'
        '  </TraceData>\n'
        '</Trace>\n'
    )
def detect_vendor(head: str) -> str:
    """从 XML 头部文本识别 PLC 厂商：omron / inovance / unknown"""
    if "OMRON" in head or "Sysmac Studio" in head or "ia.omron.com" in head:
        return "omron"
    if "Inovance" in head or "InoProShop" in head:
        return "inovance"
    return "unknown"
def extract_var_types(source: Path, encoding: str, var_names: list[str]) -> dict[str, str]:
    """从 XML 声明块提取变量基础类型（INT/DINT/...），找不到默认 INT。
    欧姆龙 XML 声明格式:
      <Variable name="m_nNextAutoStep">
        <Type><TypeName>ARRAY[0..128] OF INT</TypeName></Type>
      </Variable>
    """
    declared: dict[str, str] = {}
    needed: set[str] = set()
    for name in var_names:
        base = re.sub(r"\[[^\]]*\]", "", name)
        needed.add(base)
        parts = base.split(".")
        for i in range(len(parts), 0, -1):
            needed.add(".".join(parts[:i]))
    try:
        with source.open("r", encoding=encoding, errors="strict", newline=None) as stream:
            cur_var: str | None = None
            for line in stream:
                m = re.search(r'<Variable\s+name="([^"]+)"', line)
                if m:
                    cur_var = m.group(1)
                if cur_var and cur_var in needed:
                    m2 = re.search(r"<TypeName>\s*([^<]+?)\s*</TypeName>", line)
                    if m2:
                        type_str = m2.group(1).strip()
                        m3 = re.search(r"\bOF\s+(\w+)$", type_str, re.I)
                        declared[cur_var] = (m3.group(1).upper() if m3 else type_str.upper())
                if "</Variable>" in line:
                    cur_var = None
    except (UnicodeError, OSError):
        pass
    result: dict[str, str] = {}
    for name in var_names:
        base = re.sub(r"\[[^\]]*\]", "", name)
        t = declared.get(base)
        if not t:
            parts = base.split(".")
            for i in range(len(parts), 0, -1):
                t = declared.get(".".join(parts[:i]))
                if t:
                    break
        result[name] = t or "INT"
    return result
def build_omron_csv(variables: list[str], types: dict[str, str]) -> str:
    """生成 OMRON DataTrace CSV 文本（CRLF 行尾，不含 BOM，BOM 由调用方写入）。"""
    header_lines = [
        "TraceType,Continuous",
        "TriggerType,",
        "SamplePeriod,0:0:10:0:0",
        "SampleRate,1",
        "ExternalFileStorage,False",
        "MaxSamplesPerFile,10000",
        "MaxDataSetSize,100",
        "TargetDirectory,C:\\OMRON\\Data\\DataTrace\\",
        "FilePrefix,",
        "",
        "SampleTimingType,cyclic",
        "TaskName,BaseControl",
        "SamplingInterval,1",
        "Triggers,False:::",
        "PostTriggerDataRatio,100",
        "StartOnRun,False",
        "EnableRealTimeDisplay,False",
        "BehaviorWhenMax,Stop",
        "",
        "TriggerRecordTime,0",
        "StartRawTime,0",
    ]
    data_header = io.StringIO()
    csv.writer(data_header).writerow(["Index", "Date", "ClockTime", "RawTime", "TraceSampID",
                                     *(f"{name}:{types.get(name, 'INT')}" for name in variables)])
    return "\r\n".join(header_lines) + "\r\n" + data_header.getvalue()


def _check_outputs(out, source, names, overwrite):
    for name in names:
        path = out / name
        if path.resolve() == source:
            raise ExtractionError("输出文件与源码同名同路径，已拒绝覆盖源码。")
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ExtractionError(f"输出路径不是普通文件：{path}")
        if path.exists() and not overwrite:
            raise ExtractionError(f"输出文件已存在：{path}。请换目录，或加 --overwrite。")


def _publish(out, files, overwrite):
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".step-variables-", dir=out) as temp:
        stage = Path(temp)
        for name, contents in files.items():
            (stage / name).write_bytes(contents)
        published = []
        try:
            for name in files:
                target = out / name
                if overwrite and target.exists():
                    backup = stage / (name + ".old")
                    copyfile(target, backup)
                    (stage / name).replace(target)
                    published.append((target, backup))
                else:
                    with target.open("xb"):
                        pass
                    published.append((target, None))
                    (stage / name).replace(target)
        except OSError:
            for target, backup in reversed(published):
                if backup is None:
                    target.unlink()
                else:
                    backup.replace(target)
            raise


def extract(source: Path | str, pattern: str, out: Path | str,
            mode: str = "glob", case_sensitive: bool = False,
            encoding: str = "auto", overwrite: bool = False,
            decode_escapes: str = "auto") -> dict:
    """Extract static (constant-index) variable names to static_variables.txt.
    Returns a summary dict. Does not execute any source code.
    """
    source = Path(source).expanduser().resolve()
    out = Path(out).expanduser().resolve()
    if not source.is_file():
        raise ExtractionError(f"源码文件不存在：{source}")
    if out.exists() and not out.is_dir():
        raise ExtractionError(f"输出路径不是文件夹：{out}")
    before = source.stat()
    matcher = compile_name_pattern(pattern, mode, case_sensitive)
    actual_encoding = choose_encoding(source, encoding)
    try:
        with source.open('r', encoding=actual_encoding, errors='strict') as head_stream:
            head = head_stream.read(4096).lstrip()
    except UnicodeError as exc:
        raise ExtractionError(f"无法用 {actual_encoding} 解码源码；请用 --encoding 指定实际编码。") from exc
    decoder = SourceDecoder(decode_escapes, xml_source=head.startswith('<'))
    vendor = detect_vendor(head)
    trace_name = source.stem + (".csv" if vendor == "omron" else ".trace")
    _check_outputs(out, source, (VARIABLE_LIST, trace_name), overwrite)
    digest = file_sha256(source)
    records: dict[str, VariableRecord] = {}
    lines_read = 0
    total = 0
    try:
        with source.open("r", encoding=actual_encoding, errors="strict", newline=None) as stream:
            for lines_read, line in enumerate(stream, 1):
                for name, raw, start, end, decoded_raw, method in decoder.iter_matches(line, matcher):
                    key = name if case_sensitive else name.casefold()
                    if key not in records:
                        records[key] = VariableRecord(name, 0, lines_read, start + 1, lines_read)
                    record = records[key]
                    record.count += 1
                    record.last_line = lines_read
                    record.spellings.add(name)
                    total += 1
    except UnicodeError as exc:
        raise ExtractionError(
            f"无法用 {actual_encoding} 解码源码。请核对编码；例如加 --encoding gb18030。未丢弃或替换坏字节。"
        ) from exc
    ordered = sorted(records.values(), key=lambda r: natural_key(r.name))
    static_names = [r.name for r in ordered if not index_kind(r.name).startswith("符号")]
    dynamic_count = len(ordered) - len(static_names)
    if not static_names:
        raise ExtractionError(f"没有匹配的静态变量。跳过的动态下标：{dynamic_count} 个。")
    if vendor == "omron":
        var_types = extract_var_types(source, actual_encoding, static_names)
        config = build_omron_csv(static_names, var_types).encode("utf-8-sig")
    else:
        config = build_trace_xml(static_names).encode("utf-8")

    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ExtractionError("扫描期间源码发生变化，本次结果未发布。请关闭修改源码的程序后重试。")
    _check_outputs(out, source, (VARIABLE_LIST, trace_name), overwrite)
    _publish(out, {VARIABLE_LIST: ("\n".join(static_names) + "\n").encode("utf-8"),
                   trace_name: config}, overwrite)
    summary = {
        "tool": "plc-variable-extractor", "version": VERSION,
        "source_file": str(source), "source_sha256": digest, "source_bytes": before.st_size,
        "encoding": actual_encoding, "pattern_mode": mode, "input_pattern": pattern,
        "compiled_pattern": matcher.pattern, "case_sensitive": case_sensitive,
        "vendor": vendor,
        "scope": "all_source_text_including_comments_and_strings",
        "decode_escapes": decode_escapes, "decoding": decoder.stats,
        "line_numbers": "1-based original physical source lines; columns are Unicode character positions",
        "lines_scanned": lines_read, "unique_variables": len(ordered),
        "total_occurrences": total, "dynamic_index_references": dynamic_count,
        "static_variables": len(static_names),
        "trace_file": trace_name, "trace_variables": len(static_names),
        "llm_used": False, "input_modified": False,
        "outputs": [VARIABLE_LIST, trace_name],
        "limitations": [
            "按文本显式引用枚举，不证明变量是可执行代码中的真实步序变量。注释、字符串和未调用代码也可被统计。",
            "不推导声明中的全部数组元素，不求动态下标，不做功能块形实参或别名展开。",
            "不拼接跨物理行/逻辑换行的变量名；嵌套数组下标不展开。",
            "auto模式仅对XML文本/属性实体及JSON形态行中的合法字符串做一层解码；不是完整容器验证器。CDATA不做XML实体解码。",
            "不解压二进制工程、Base64或任意多层嵌套转义；无效JSON字符串保持原样并计入诊断。",
            "按完整引用匹配，不从其他对象的限定名称中截取后缀。忽略大小写时保留首次出现的规范化写法。",
            "仅输出固定下标变量到 static_variables.txt；符号/表达式下标不输出，只在统计中计数。",
        ],
        "warnings": (["存在动态/符号下标：已跳过，未写入 static_variables.txt。"] if dynamic_count else [])
        + (["有JSON字符串不能解码，已按原文保留；详见 decoding 统计。"] if decoder.stats['invalid_json_strings_left_raw'] else [])
        + (["有未配对Unicode代理字符未解码，保持原文。"] if decoder.stats['unpaired_surrogates_left_raw'] else []),
    }
    return summary


def add_arguments(parser):
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--source", required=True, type=Path, help="PLC源码路径：XML、ST或TXT等文本文件")
    match_args = parser.add_mutually_exclusive_group(required=True)
    match_args.add_argument("--pattern", help="完整变量名或通配模式，支持 re: 正则前缀")
    match_args.add_argument("--regex", help="Python正则表达式，对完整变量名做fullmatch")
    parser.add_argument("--out", type=Path, default=Path("outputs/variables"), help="输出文件夹")
    parser.add_argument("--encoding", default="auto", help="auto/BOM/XML声明/UTF-8；也可手动指定gb18030等")
    parser.add_argument("--case-sensitive", action="store_true", help="区分大小写；默认不区分")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖本工具管理的结果文件")
    parser.add_argument("--decode-escapes", choices=("auto", "none"), default="auto",
                        help="auto:解码XML实体/JSON字符串转义并保留原始位置；none:按v1.0纯原文扫描")


def run(args) -> int:
    pattern = args.regex if args.regex is not None else args.pattern
    mode = "regex" if args.regex is not None else "glob"
    if mode == "glob" and pattern.startswith(("re:", "regex:")):
        pattern, mode = pattern.split(":", 1)[1], "regex"
    try:
        summary = extract(args.source, pattern, args.out, mode=mode,
                          encoding=args.encoding, case_sensitive=args.case_sensitive, overwrite=args.overwrite,
                          decode_escapes=args.decode_escapes)
    except (ExtractionError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(f"匹配模式：{summary['input_pattern']}")
    print(f"厂商：{summary['vendor']}")
    print(f"不同变量：{summary['unique_variables']}；固定下标变量：{summary['static_variables']}；动态/表达式引用：{summary['dynamic_index_references']}")
    print(f"输出文件：{args.out.expanduser().resolve() / 'static_variables.txt'}")
    print(f"输出文件：{args.out.expanduser().resolve() / summary['trace_file']}")
    for warning in summary["warnings"]:
        print("注意：" + warning)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按名称模式提取步序变量并生成采集配置")
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
