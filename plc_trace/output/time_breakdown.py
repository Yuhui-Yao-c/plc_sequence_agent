"""Render the entry-one time breakdown. All row facts are keyed by both Trace labels."""
import re
from collections import Counter
from copy import copy
from math import ceil
from pathlib import Path
from unicodedata import east_asian_width

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter, quote_sheetname
from openpyxl.worksheet.hyperlink import Hyperlink

from plc_trace.description.trace_description_report import annotation_evidence_text, waiting_role
from plc_trace.trace.trace_source_evidence import EVIDENCE_FILE, RELATION_TEXT, read_trace_evidence


# Widths, palette, freeze and data height follow the BIFF template.
COLUMNS = (
    ('构成要素', 18.796875), ('动作时间', 6.3984375), ('完成时间', 7.19921875),
    ('动作描述', 39.3984375), ('描述来源', 10), ('时长（ms）', 15.8984375),
    ('目标时长', 15.8984375), ('开始标签', 24.09765625), ('结束标签', 29),
    ('动作号', 7.19921875), ('同步号', 7.19921875), ('工序号', 12.19921875),
    ('等待位标识（1：等待步）', 16.296875), ('备注', 33.296875), ('证据链', 72),
)
STEP_HEIGHT = 13.2
FONT = Font(name='Microsoft YaHei Light', size=9)
BOLD = Font(name=FONT.name, size=9, bold=True)
RED = Font(name=FONT.name, size=9, bold=True, color='FFFF0000')
THIN = Side(style='thin', color='FF000000')
GRID = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal='center', vertical='center')
LEFT = Alignment(horizontal='left', vertical='center')
GROUP = Alignment(horizontal='center', vertical='center', wrap_text=True)
FILLS = {name: PatternFill('solid', fgColor='FF'+color) for name, color in (
    ('表头', 'CCFFFF'), ('端列', 'CC99FF'), ('构成要素', 'FFCC99'),
    ('待料', 'FFFF00'), ('堵料', '969696'))}


def _cell(sheet, row, col, value=None):
    cell = sheet.cell(row, col, value)
    if isinstance(value, str):
        cell.data_type = 's'  # A PLC comment starting with '=' is literal source text.
    cell.font, cell.border, cell.alignment = FONT, GRID, CENTER
    return cell


def _width(text):
    return sum(2 if east_asian_width(c) in ('W', 'F') else 1 for c in text)


def _details(book, original, start, end, heading, text):
    if '文本详情' not in book:
        sheet = book.create_sheet('文本详情')
        for idx, (name, width) in enumerate((('主表行', 10), ('开始标签', 34), ('结束标签', 34),
                                             ('字段', 18), ('完整内容', 112)), 1):
            cell = _cell(sheet, 1, idx, name)
            cell.fill = FILLS['表头']
            sheet.column_dimensions[cell.column_letter].width = width
        sheet.freeze_panes = 'A2'
    sheet = book['文本详情']
    first = sheet.max_row + 1
    # Keep long evidence readable within Excel's maximum row height and cell length.
    pieces, lines, height = [], [], 0
    for line in text.split('\n'):
        for pos in range(0, max(1, len(line)), 1000):
            part = line[pos:pos + 1000]
            n = max(1, ceil(_width(part) / 105))
            if lines and height + n > 24:
                pieces.append(('\n'.join(lines), height))
                lines, height = [], 0
            lines.append(part)
            height += n
    pieces.append(('\n'.join(lines), height))
    for row, (piece, height) in enumerate(pieces, first):
        for col, val in enumerate((original.row, start, end, heading, piece), 1):
            cell = _cell(sheet, row, col, val)
            cell.alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)
        back = sheet.cell(row, 1)
        back.hyperlink = Hyperlink(ref=back.coordinate,
                                   location=f'{quote_sheetname(original.parent.title)}!{original.coordinate}')
        sheet.row_dimensions[row].height = min(409, max(20, 15 * height + 5))
    original.hyperlink = Hyperlink(ref=original.coordinate, location=f'{quote_sheetname(sheet.title)}!E{first}',
                                   tooltip='查看完整文本及证据')
    font = copy(original.font)
    font.underline = 'single'
    original.font = font
    if '\n' in text:
        alignment = copy(original.alignment)
        alignment.vertical = 'top'
        original.alignment = alignment


def _header(ws, proc, digest):
    ws.title = '时序拆解'
    ws.sheet_view.zoomScale = 70
    ws.freeze_panes = 'A3'
    ws.sheet_format.defaultRowHeight = STEP_HEIGHT
    for col, (name, width) in enumerate(COLUMNS, 1):
        dim = ws.column_dimensions[get_column_letter(col)]
        dim.width, dim.hidden = width, col in (2, 3)
        top = _cell(ws, 1, col, {1: '动作分解', 8: 'tag'}.get(col))
        top.fill = FILLS['构成要素' if col == 1 else '表头']
        cell = _cell(ws, 2, col, name)
        cell.fill = FILLS['端列' if name in ('构成要素', '备注', '证据链') else '表头']
        if col not in (10, 11, 12, 13):
            cell.alignment = LEFT
    ws.merge_cells('B1:C1')
    ws.merge_cells('H1:I1')
    ws.row_dimensions[1].height = ws.row_dimensions[2].height = STEP_HEIGHT
    notes = {'A1': f'{proc}。输入状态对 SHA256 前16位 {digest}。按工位和完整步序变量分块。',
             'B2': '模板的独立动作时间。本表记录观测停留时长，见时长（ms）。',
             'C2': '多次采集按状态对归并，无法给出单次执行的累计完成时间。',
             'F2': '同一完整起止标签在各次观测中的起始状态停留时长中位数。单位为毫秒。',
             'G2': '目标节拍需要独立的工程定额。',
             'M2': '1 表示有依据的等待步。黄色表示待料，灰色表示堵料，表示源码定义的步职责。'
                   '普通等待无色，无法判定时留空。颜色不计量真实缺料或拥堵损失。',
             'O2': '按起止标签追溯 Trace 跳变、源码迁移和描述依据。单击有下划线的单元格阅读完整文本。'}
    for coord, note in notes.items():
        ws[coord].comment = Comment(note, 'PLC Trace')


def _evidence(pair, desc, audits):
    start, end = pair['开始标签'], pair['结束标签']
    obs = f'Trace 观测 {start} → {end}'
    if count := pair.get('发生次数'):
        obs += f'，出现 {count} 次'
    if duration := pair.get('中位数时长ms'):
        obs += f'，起始步停留中位数 {duration} ms'
    if pair.get('估算时长次数') not in (None, '', '0'):
        obs += f'（其中 {pair["估算时长次数"]} 次时长经估算）'
    audit = ' / '.join(name for name in ('trace_jumps.csv', 'trace_segments.csv') if name in audits)
    if 'trace_jumps.csv' in audits and pair.get('样例跳变索引') not in (None, ''):
        capture = pair.get('样例采集ID')
        audit += '，首个跳变' + (f'采集 {capture}，' if capture else '') + f'样本索引 {pair["样例跳变索引"]}'
    relation = RELATION_TEXT.get(pair.get('源码关系'), '源码关系待核')
    loc = '，'.join(s for s in (pair.get('证据POU'), pair.get('证据节点')) if s)
    parts = [obs, audit or '观测依据仅有状态对 CSV，跳变和稳定段台账缺失', relation + (f'〔{loc}〕' if loc else '')]
    if EVIDENCE_FILE in audits:
        parts.append(f'{EVIDENCE_FILE} → pairs → {start}|||{end}')
    else:
        parts.append('本轮源码证据索引缺失')
    if guard := pair.get('原始条件'):
        parts.append('源码门控 ' + guard)
    if variables := desc.get('动作变量') or pair.get('动作变量'):
        parts.append('源码动作变量（引用） ' + variables)
    detail = desc.get('证据')
    if detail:
        parts.append(detail)
    elif desc.get('动作描述'):
        parts.append('描述依据 ' + (desc.get('证据引用') or '定位信息未提供，待核'))
    else:
        parts.append('动作描述待核：' + (desc.get('待核原因') or desc.get('描述状态') or '缺少已确认的描述依据'))
    if role := desc.get('等待判定'):
        parts.append(f'{role}依据：{desc["等待依据"]}')
    return '\n'.join(parts)


def _channel_sort(name):
    return tuple(int(p) if i % 2 else p for i, p in enumerate(re.split(r'([+-]?\d+)', name.casefold()))), name


def restore_annotation_details(run, descriptions):
    """Re-render a saved A-only or older run from its own selected annotations."""
    for key, info in (read_trace_evidence(run) or {}).items():
        text = info.get('description')
        if not text or not info.get('description_source', '').startswith('A档'):
            continue
        start, end = key.split('|||', 1)
        row = descriptions.setdefault((start, end), {})
        if row.get('动作描述') not in (None, '', text) or row.get('描述来源', '').startswith('B档'):
            continue
        role, basis = waiting_role(info, start)
        row.update({'动作描述': text, '描述来源': info['description_source'], '描述状态': '源码注释',
                    '证据': annotation_evidence_text(info), '等待判定': role, '等待依据': basis})


def write_breakdown(groups, descriptions, out, proc, digest, run):
    book = Workbook()
    sheet = book.active
    _header(sheet, proc, digest)
    audits = {name for name in ('trace_jumps.csv', 'trace_segments.csv', EVIDENCE_FILE) if (Path(run) / name).is_file()}
    review_rows = b_rows = measured = 0
    waits = Counter()
    r = 3
    for station, rows in groups:
        channels = list(rows.groupby('_步序变量', sort=False))
        channels.sort(key=lambda group: _channel_sort(group[0]))
        total = None
        for idx, (channel, records) in enumerate(channels):
            if idx:
                for gap in (r, r + 1):
                    sheet.row_dimensions[gap].height = 10
                r += 2
            first = r
            for pair in records.fillna('').to_dict('records'):
                start, end = pair['开始标签'], pair['结束标签']
                desc = descriptions.get((start, end), {})
                text = desc.get('动作描述', '')
                status, source = desc.get('描述状态', ''), desc.get('描述来源', '')
                origin = ''
                if text and (status == '源码注释' or (not status and source.startswith('A档') and '注释' in source)):
                    origin = '注释'
                if text and source.startswith('B档') and status in ('', 'LLM推断待核验'):
                    origin = 'LLM'
                    b_rows += 1
                role = desc.get('等待判定') if origin and desc.get('等待依据') else ''
                if role:
                    waits[role] += 1
                review = pair.get('源码关系') != 'direct' or status != '源码注释'
                review_rows += int(review)
                notes = [p for p in pair.get('备注', '').split(' | ')
                         if p and p != '无步注释，等待LLM综合上下文描述']
                if reasoning := desc.get('推断依据'):
                    notes.append(f'{desc.get("证据等级", "")}：{reasoning}')
                if supplement := desc.get('注释补充'):
                    notes.append(supplement)
                if role:
                    notes.append(f'{role}（{desc["等待依据"]}）')
                if review:
                    notes.append('【' + (status or '描述待核验') + '】')
                duration = pair.get('中位数时长ms', '')
                ms = float(duration) if duration != '' else None
                if ms is not None:
                    total = (total or 0) + ms
                    measured += 1
                values = {4: text or None, 5: origin or None, 6: ms, 8: start, 9: end,
                          13: 1 if role else None, 14: ' | '.join(notes) or None, 15: _evidence(pair, desc, audits)}
                for col, (heading, width) in enumerate(COLUMNS, 1):
                    value = values.get(col)
                    cell = _cell(sheet, r, col, value)
                    if col >= 4 and role in ('待料', '堵料'):
                        cell.fill = FILLS[role]
                    if col in (2, 3, 6, 7):
                        cell.number_format = '0.00##'
                    if col in (4, 14, 15):
                        cell.alignment = LEFT
                    if (col == 5 and origin == 'LLM') or (col == 14 and review):
                        cell.font = RED
                    if isinstance(value, str) and ('\n' in value or _width(value) > width - 1):
                        _details(book, cell, start, end, heading, value)
                sheet.row_dimensions[r].height = STEP_HEIGHT
                r += 1
            # For an unsplit station the template's merged group includes its total.
            last = r if len(channels) == 1 else r - 1
            label = _cell(sheet, first, 1, station if len(channels) == 1 else f'{station}\n{channel}')
            label.fill, label.font, label.alignment = FILLS['构成要素'], BOLD, GROUP
            if last > first:
                sheet.merge_cells(start_row=first, end_row=last, start_column=1, end_column=1)
            if len(channels) > 1 and last - first < 2:
                _details(book, label, records.iloc[0]['开始标签'], records.iloc[0]['结束标签'], '构成要素', label.value)
        for col in range(2, len(COLUMNS) + 1):
            cell = _cell(sheet, r, col, {4: '总时长', 6: round(total, 4) if total is not None else None,
                                        14: '本工位各行停留中位数之和，不能作为单次循环时间。'}.get(col))
            cell.font = BOLD
            if col == 6:
                cell.number_format = '0.00##'
            if col in (4, 14):
                cell.alignment = LEFT
        if len(channels) > 1:
            label = _cell(sheet, r, 1, station)
            label.fill, label.font = FILLS['构成要素'], BOLD
        sheet.row_dimensions[r].height = 13.8
        r += 1
    book.save(out)
    return {'trace_review_rows': review_rows, 'b_desc_rows': b_rows,
            'trace_duration_backfilled': measured > 0, 'wait_rows': dict(waits)}
