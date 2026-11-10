"""Render the observed Trace workbook and its long-text details."""
import argparse
import csv
import json
import re
from copy import copy
from math import ceil
from pathlib import Path
from unicodedata import east_asian_width

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import quote_sheetname
from openpyxl.worksheet.hyperlink import Hyperlink

from station_identity import source_station_displays
from utils import infer_proc_name, load_station_identity
from workflow import fingerprint


def clean_doc(text: str) -> str:
    """Documentation may wrap a GrpComment XML fragment with embedded monitor
    JSON ( <Sub/>{k:...} ); keep only the plain zh name before <Sub/>."""
    t = str(text)
    m = re.search(r'<GrpComment_zh-cn>(.*?)</GrpComment_zh-cn>', t, re.S)
    if m:
        t = m.group(1)
    i = t.find('<Sub/>')
    if i >= 0:
        t = t[:i]
    t = re.sub(r'<[^>]+>', '', t)
    return re.sub(r'\s+', '', t).strip()


def load_var_docs(run: str):
    """variable root name -> cleaned Chinese documentation (source facts)."""
    path = Path(run) / 'variable_documentation.json'
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    return {name: text for name, raw in (data.get('stations') or data.get('all', {})).items()
            if (text := clean_doc(raw))}


def _station_code(root: str) -> str:
    """Station number from the state-variable root.
    omron native structs 'stEM310' -> 'EM310' (strip leading st/St/ST only when
    followed by an uppercase letter); 'udt_EM01'/'udt_EM07_1' -> 'EM01'/'EM07_1'
    (strip udt_ prefix, v9.16); SC04-style roots ('ST06', 'HJXHX_ST06')
    are already the station number and kept verbatim.
    Returns '' for NaN/None/empty so it never leaks into display names."""
    s = str(root)
    if not s.strip() or s.lower() in ('nan', 'none'):
        return ''
    # v9.16: udt_EMxx -> EMxx (omron global user-data-type station structs)
    m = re.match(r'^udt_(EM\d+(?:_\d+)?)$', s)
    if m:
        return m.group(1)
    m = re.match(r'^[sS][tT]([A-Z].*)$', s)
    return m.group(1) if m else s


def workstation(comp: str, var_root: str = '', var_docs: dict = None,
                si_map: dict = None) -> str:
    """Resolve a workstation display name for a row.

    v9.20 priority:
      0. POU path in 构成要素 (EMxx_name::SCxx_...) — direct extraction,
         handles codesys array-type state machines where 构成要素 carries
         the full POU path.  This is the most specific source.
      1. station_identity.json — provides code, doc, sort_key.
         For array-type controllers (m_nNextAutoStep[N]), identity keys include
         the array index so each index resolves to a distinct workstation.
      2. 构成要素 SC04 Chinese suffix — sub-station granularity refinement.
      3. var_docs fallback (v9.16 path).
      4. bare code + section fallback (v9.16 path).
    """
    var_docs = var_docs or {}
    si_map = si_map or {}
    var_root = str(var_root) if pd.notna(var_root) and var_root else ''
    parts = [p.strip() for p in str(comp).split(';')]
    cjk = re.compile(r'[\u4e00-\u9fff]')

    # v9.20: POU path extraction (EMxx_name::SCxx_...)
    for p in parts:
        m = re.match(r'(EM\d+)_(.+?)(?:_L\d+.*)?::', p)
        if m and cjk.search(m.group(2)):
            code = m.group(1)
            name = re.sub(r'_L\d+.*$', '', m.group(2))
            return f'{code}_{name}'

    # v9.17/v9.20: station_identity — try indexed key first, then bare root.
    si_entry = si_map.get(var_root) if var_root else None
    if not si_entry and var_root:
        bare_root = re.sub(r'\[\d+\]', '', var_root)
        if bare_root != var_root:
            si_entry = si_map.get(bare_root)
    code = si_entry['code'] if si_entry else (_station_code(var_root) if var_root else '')

    # Sub-station granularity: SC04 Chinese suffix is finer than station_identity.
    # Use the station_identity CODE (never lose identity) + the sub-station name.
    best = None
    for p in parts:
        m = re.match(r'SC04_(?:RUN\d*_?|Run\d*_?)\s*(.+)', p)
        if m and m.group(1).strip():
            name = m.group(1).strip()
            if cjk.search(name):
                return f'{code}_{name}' if code else name
            best = best or name
        elif p.startswith('SC04') and cjk.search(p):
            return p

    # v9.17: station_identity display (station-level, e.g. EM04_长边清洗工位)
    if si_entry and si_entry.get('doc'):
        return si_entry['display']

    # v9.16 fallback: var_docs (station-level Chinese name)
    if var_root:
        root = var_root.split('.')[0].split('[')[0]
        if root in var_docs:
            zh = var_docs[root]
            return f'{code}_{zh}' if code else zh
    # non-Chinese best (e.g. '1' from 'SC04_Run1') as last resort before bare code
    if best:
        return f'{code}_{best}' if code else best
    # v9.16: bare section name (e.g. 'SC04_Run') with no Chinese suffix and no
    # var_docs hit — prefix the station code from the variable root so each EM
    # is a distinct workstation block instead of collapsing 12 EMs into one
    # mixed 'SC04_Run' block with broken step ordering.
    if code:
        sec = parts[0] if parts and parts[0].startswith('SC04') else ''
        return f'{code}_{sec}' if sec else code
    for p in parts:
        if p.startswith('SC04'):
            return p
    return parts[0] if parts else str(comp)


def _em_sort_key(name: str):
    """Numeric sort key for workstation names like 'EM07_1_长边焊接1工位'
    or 'ST06_铜上盘' — extracts (prefix_num, sub_num) so EM01<EM02<…<EM10
    and EM07_1<EM07_2; non-matching names sort last stably."""
    m = re.search(r'(?:EM|ST)(\d+)(?:_(\d+))?', str(name))
    if m:
        return (int(m.group(1)), int(m.group(2)) if m.group(2) else 0)
    return (10 ** 9, 0)


def cell_str(x) -> str:
    try:
        if pd.isna(x):
            return ""
    except (TypeError, ValueError):
        pass
    return str(x)


def _text_cell(ws, row, column, value):
    cell = ws.cell(row=row, column=column, value=value)
    if isinstance(value, str):
        # Keep source annotations such as ====判断步==== as literal text.
        cell.data_type = 's'
    return cell


def _wrapped_lines(text, width):
    lines = 0
    for line in text.splitlines():
        length = sum(2 if east_asian_width(ch) in ('W', 'F') else 1 for ch in line)
        lines += max(1, ceil(length / width))
    return lines


def pou_station(pou) -> str:
    first = cell_str(pou).split(";")[0].strip()
    area = first.split("::")[0].strip()
    return area or "未分组"


def var_of(label: str):
    m = re.match(r'\s*([A-Za-z_]\w*(?:\[[^\]]*\])?(?:\.\w+(?:\[[^\]]*\])?)*)\s*=', str(label))
    return m.group(1) if m else None


def _trace_channel_sort_key(name):
    parts = re.split(r'([+-]?\d+)', name.casefold())
    natural = tuple(int(part) if i % 2 else part for i, part in enumerate(parts))
    return natural, name


STEP_HEIGHT = 28
_BODY = Font(name='Arial', size=9, color='23364B')
_HEADER = Font(name='Arial', size=10, bold=True, color='FFFFFF')
_TITLE = Font(name='Arial', size=14, bold=True, color='20364A')
_MUTED = Font(name='Arial', size=9, color='50657A')
_TOTAL = Font(name='Arial', size=9, bold=True, color='20364A')
_REVIEW = Font(name='Arial', size=9, color='9C3829')
_LLM = Font(name='Arial', size=9, color='FF0000', bold=True)
_HEADER_FILL = PatternFill('solid', fgColor='25435D')
_TOTAL_FILL = PatternFill('solid', fgColor='E8F0F7')
_GROUP_FILL = PatternFill('solid', fgColor='F2F7FB')
_BORDER = Border(bottom=Side(style='thin', color='DBE4EC'), right=Side(style='thin', color='E5EBF1'))
_LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)
_CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
_TOP = Alignment(horizontal='center', vertical='top', wrap_text=True)
_NUMBER = Alignment(horizontal='right', vertical='center', indent=1)


def _heading(ws, row, column, value, width):
    cell = _text_cell(ws, row, column, value)
    cell.fill, cell.font, cell.alignment = _HEADER_FILL, _HEADER, _CENTER
    ws.column_dimensions[cell.column_letter].width = width


def _detail_sheet(book):
    ws = book.create_sheet('文本详情')
    ws.sheet_view.showGridLines = False
    fields = (('主表行', 10), ('开始标签', 31), ('结束标签', 31), ('字段', 18), ('完整内容', 112))
    for column, (label, width) in enumerate(fields, 1):
        _heading(ws, 1, column, label, width)
    ws.row_dimensions[1].height = 25
    ws.freeze_panes = 'A2'
    return ws


def _link_detail(detail, row, original, start, end, field):
    for column, value in enumerate((original.row, start, end, field, original.value), 1):
        cell = _text_cell(detail, row, column, value)
        cell.font, cell.alignment, cell.border = _BODY, _LEFT, _BORDER
    back = detail.cell(row, 1)
    back.hyperlink = Hyperlink(ref=back.coordinate,
                               location=f'{quote_sheetname(original.parent.title)}!{original.coordinate}',
                               tooltip='返回步序采集表')
    back.font = Font(name='Arial', size=9, underline='single', color='235B87')
    original.hyperlink = Hyperlink(ref=original.coordinate, location=f'{quote_sheetname(detail.title)}!E{row}',
                                   tooltip='查看完整文本')
    link_font = copy(original.font)
    link_font.underline = 'single'
    original.font = link_font
    alignment = copy(original.alignment)
    alignment.vertical = 'top'
    original.alignment = alignment
    detail.row_dimensions[row].height = min(409, max(STEP_HEIGHT, 15 * _wrapped_lines(original.value, 106) + 8))


def _descriptions(run):
    path = run / 'action_desc_final.csv'
    if not path.is_file():
        path = run / 'action_desc_a.csv'
    if not path.is_file():
        return {}
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return {(row['开始标签'], row['结束标签']): row for row in csv.DictReader(stream)}


def generate(pairs, out=None, group_by='auto', proc_name=None, run=None, sheet='trace步序采集表'):
    """Read the Trace CSV and optional description files, then export the workbook."""
    pairs = Path(pairs)
    run = Path(run) if run else pairs.parent
    summary = run / 'trace_pairs_summary.json'
    meta = json.loads(summary.read_text(encoding='utf-8-sig')) if summary.is_file() else {}
    proc_name = infer_proc_name(None, run, proc_name or meta.get('proc_name'))
    out = Path(out) if out else pairs.parent / f'{proc_name}_trace步序采集表.xlsx'
    indexed_stations = meta.get('source_evidence_audit', {}).get('indexed_stations', {})
    identity = load_station_identity(run)
    channel_identity = {name: entry for name, entry in identity.items() if entry.get('source') == 'pou_evidence'}
    df = pd.read_csv(pairs, dtype=str, encoding='utf-8-sig', keep_default_na=False)
    required = {'开始标签', '结束标签', '中位数时长ms'}
    if missing := required - set(df.columns):
        raise ValueError(f'Trace 状态对缺少列：{", ".join(sorted(missing))}')
    if group_by == 'auto':
        group_by = '构成要素' if '构成要素' in df else '证据POU'
    if group_by not in df.columns:
        raise ValueError(f'Trace 状态对缺少分组列：{group_by}')

    df['_变量'] = df['开始标签'].map(var_of)
    df['_步序变量'] = df['_变量'].fillna(df.get('控制器', '')).fillna('').str.strip()
    df['_变量根'] = df['_步序变量'].str.split('.', n=1, regex=False).str[0]
    df['_序'] = df['开始标签'].str.extract(r'=\s*([+-]?\d+)\s*$', expand=False).fillna(str(10 ** 9)).map(int)
    if group_by == '构成要素':
        docs = load_var_docs(run)
        df['_工位'] = [
            indexed_stations.get(channel) or channel_identity.get(channel, {}).get('display')
            or workstation(comp, root, docs, identity)
            for channel, root, comp in zip(df['_步序变量'], df['_变量根'], df[group_by])
        ]
        if '证据POU' in df:
            names = source_station_displays(zip(df['_工位'], df['_步序变量'], df['证据POU']), identity)
            df['_工位'] = df['_工位'].replace(names)
    elif group_by == '证据POU':
        df['_工位'] = df['证据POU'].map(pou_station)
    else:
        df['_工位'] = df['控制器'].map(lambda value: value.strip() or '未分组')

    def sort_key(channel, root, station):
        if group_by == '构成要素' and channel in indexed_stations:
            return _em_sort_key(station)
        entry = channel_identity.get(channel) if group_by == '构成要素' else None
        entry = entry or identity.get(root) or identity.get(re.sub(r'\[\d+\]', '', root))
        return tuple(entry['sort_key']) if entry and 'sort_key' in entry else _em_sort_key(station)

    df['_工位序'] = [sort_key(channel, root, station)
                  for channel, root, station in zip(df['_步序变量'], df['_变量根'], df['_工位'])]
    workstations = df.sort_values(['_工位序', '_步序变量', '_序'], kind='stable').groupby('_工位', sort=False)
    has_subgroups = workstations['_步序变量'].nunique().gt(1).any()

    descriptions = _descriptions(run)
    book = Workbook()
    ws = book.active
    ws.title = sheet
    ws.sheet_view.showGridLines = False
    ws.column_dimensions['A'].width = 3
    layout = (('构成要素', 33 if has_subgroups else 24), ('动作描述', 38), ('描述来源', 10), ('时长(ms)', 12),
              ('动作变量（引用）', 31), ('开始标签', 30), ('结束标签', 30), ('备注', 36), ('证据', 51))
    last_col = len(layout) + 1
    for column, (heading, width) in enumerate(layout, 2):
        _heading(ws, 3, column, heading, width)
    for row in (1, 2):
        ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=last_col)
    _text_cell(ws, 1, 2, f'{proc_name} 步序采集表（Trace观测交付）').font = _TITLE
    ws.freeze_panes = 'D4'
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 25
    ws.row_dimensions[3].height = 26

    detail = None
    detail_row = 2
    review_count = 0
    b_tier_pending = False
    duration_backfilled = False
    source_counts = {}
    row_number = 4
    for station, group in workstations:
        subgrouped = group['_步序变量'].nunique() > 1
        if subgrouped:
            group = group.sort_values('_步序变量', kind='stable', key=lambda names: names.map(_trace_channel_sort_key))
            blocks = group.groupby('_步序变量', sort=False)
        else:
            blocks = [(None, group)]
        station_start = row_number
        station_duration = None
        for block_index, (channel, block) in enumerate(blocks):
            if block_index:
                for gap in range(row_number, row_number + 2):
                    ws.row_dimensions[gap].height = 10
                row_number += 2
            block_start = row_number
            for _, row in block.iterrows():
                start, end = row['开始标签'], row['结束标签']
                entry = descriptions.get((start, end), {})
                desc = (entry.get('动作描述', entry.get('A档描述', row.get('动作描述', ''))) or '').strip()
                variables = entry.get('动作变量', row.get('动作变量', ''))
                source = entry.get('描述来源') or ('A档(源码步注释)' if desc else '')
                status = entry.get('描述状态') or ('源码注释' if desc and source.startswith('A档') else '')
                review = row.get('源码关系') != 'direct' or status != '源码注释'
                review_count += int(review)
                b_tier_pending |= status in ('B档待处理', 'B档待修订')
                if source:
                    source_counts[source] = source_counts.get(source, 0) + 1
                origin = '注释' if desc and status == '源码注释' else ''
                if desc and source.startswith('B档') and status in ('', 'LLM推断待核验'):
                    origin = 'LLM'
                remarks = [part for part in row.get('备注', '').strip().split(' | ')
                           if part and part != '无步注释，等待LLM综合上下文描述']
                if reasoning := entry.get('推断依据'):
                    remarks.append(f'{entry.get("证据等级", "")}：{reasoning}')
                if origin == '注释' and entry.get('注释补充'):
                    remarks.append(entry['注释补充'])
                if review:
                    remarks.append(f'【{status or "描述待核验"}】')
                evidence = (entry.get('证据') or '分句证据待核') if origin == 'LLM' else ''
                try:
                    duration = int(row['中位数时长ms'])
                except ValueError:
                    duration = None
                if duration is not None:
                    station_duration = (station_duration or 0) + duration
                    duration_backfilled = True

                group_label = None
                if row_number == block_start:
                    group_label = f'{station}\n{channel or "步序变量未识别"}' if subgrouped else station
                values = (group_label, desc, origin, duration, variables, start, end, ' | '.join(remarks), evidence)
                for column, ((heading, width), value) in enumerate(zip(layout, values), 2):
                    cell = _text_cell(ws, row_number, column, value if value != '' else None)
                    cell.border = _BORDER
                    cell.font = _LLM if heading == '描述来源' and origin == 'LLM' else _BODY
                    cell.alignment = _NUMBER if heading == '时长(ms)' else _LEFT
                    if heading in ('构成要素', '描述来源'):
                        cell.alignment = _TOP if heading == '构成要素' and subgrouped else _CENTER
                    if heading == '构成要素':
                        cell.fill = _GROUP_FILL
                    if heading == '备注' and review:
                        cell.font = _REVIEW
                    if isinstance(value, str) and value and _wrapped_lines(value, width - 4) > 2:
                        detail = detail if detail is not None else _detail_sheet(book)
                        _link_detail(detail, detail_row, cell, start, end, heading)
                        detail_row += 1
                ws.row_dimensions[row_number].height = STEP_HEIGHT
                row_number += 1
            if subgrouped and row_number > block_start + 1:
                ws.merge_cells(start_row=block_start, start_column=2, end_row=row_number - 1, end_column=2)

        total = (station if subgrouped else None, '行时长合计', None, station_duration)
        for column in range(2, last_col + 1):
            cell = _text_cell(ws, row_number, column, total[column - 2] if column <= 5 else None)
            cell.font, cell.fill, cell.border = _TOTAL, _TOTAL_FILL, _BORDER
            cell.alignment = _NUMBER if column == 5 else _LEFT
        ws.row_dimensions[row_number].height = 23
        if not subgrouped:
            ws.merge_cells(start_row=station_start, start_column=2, end_row=row_number, end_column=2)
        row_number += 1

    a_count = sum(n for source, n in source_counts.items() if source.startswith('A档'))
    b_count = sum(n for source, n in source_counts.items() if source.startswith('B档'))
    note = (f'{len(df)} 个观测状态对 · 源码注释 {a_count} 行 · B 档归纳待核 {b_count} 行。'
            '时长为起始状态观测停留中位数。')
    if detail is not None:
        note += ' 点击带下划线的单元格查看完整文本。'
    cell = _text_cell(ws, 2, 2, note)
    cell.font, cell.alignment = _MUTED, _LEFT
    source_sha16 = fingerprint(pairs)[:16]
    book.save(out)
    return {'proc_name': proc_name, 'rows': len(df), 'workstation_groups': workstations.ngroups,
            'group_by': group_by, 'b_desc_rows': b_count, 'desc_sources': source_counts,
            'action_variable_rows': sum(bool(value.strip()) for value in df.get('动作变量', [])),
            'trace_duration_backfilled': duration_backfilled, 'trace_review_rows': review_count,
            'b_tier_pending': b_tier_pending, 'source_sha16': source_sha16, 'out': str(out),
            'long_text_cells': detail_row - 2}


def main():
    parser = argparse.ArgumentParser(description='从已有 Trace 状态对重导工作簿')
    parser.add_argument('--pairs', '--prediction', dest='pairs', required=True, help='trace_collection_pairs.csv')
    parser.add_argument('--run', help='动作描述和源码信息所在目录，缺省使用 CSV 所在目录')
    parser.add_argument('--out', help='输出 XLSX 路径')
    parser.add_argument('--proc-name', help='工序名称')
    parser.add_argument('--sheet', default='trace步序采集表')
    parser.add_argument('--group-by', choices=('auto', '构成要素', '证据POU', '控制器'), default='auto')
    args = parser.parse_args()
    try:
        stats = generate(args.pairs, args.out, args.group_by, args.proc_name, args.run, args.sheet)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
