"""Render the entry-one time breakdown. All row facts are keyed by both Trace labels."""
import csv
import json
import re
from collections import Counter, defaultdict
from copy import copy
from itertools import groupby
from math import ceil
from pathlib import Path
from unicodedata import east_asian_width

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter, quote_sheetname
from openpyxl.worksheet.hyperlink import Hyperlink

from plc_trace.description.trace_description_report import annotation_evidence_text, annotation_supplement, waiting_role
from plc_trace.trace.branch_report import OBSERVATIONS_FILE, EVIDENCE_FILE as BRANCH_EVIDENCE
from plc_trace.trace.sample_continuity import GAP_FILE
from plc_trace.trace.trace_source_evidence import EVIDENCE_FILE, RELATION_TEXT


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
             'A2': '工位标识-源码例程原名。每个完整步序变量独立分块。批注中可查通道和所属源码位置。',
             'B2': '模板的独立动作时间。本表记录观测停留时长，见时长（ms）。',
             'C2': '多次采集按状态对归并，无法给出单次执行的累计完成时间。',
             'F2': '起始步进入和离开均在同一连续采样段内时才计时。显示完整观测的中位数，单位为毫秒。空白表示没有完整计时。',
             'G2': '目标节拍需要独立的工程定额。',
             'M2': '1 表示有依据的等待步。黄色表示待料，灰色表示堵料，表示源码定义的步职责。'
                   '普通等待无色，无法判定时留空。颜色不计量真实缺料或拥堵损失。',
             'N2': '只说明本行需要理解或核对的事，例如简短注释指哪个流程、可能的中间步或时长估算。'
                   '空白表示没有额外说明。详细依据见右侧证据链。',
             'O2': '按起止标签追溯 Trace 跳变、源码迁移和描述依据。单击有下划线的单元格阅读完整文本。'}
    for coord, note in notes.items():
        ws[coord].comment = Comment(note, 'PLC Trace')


def _evidence(pair, desc, info, audits):
    start, end = pair['开始标签'], pair['结束标签']
    obs = f'Trace 观测 {start} → {end}'
    if pair.get('分支ID'):
        obs = f"{pair['本次状态']}　{start} → {end}"
        obs += f"\n源码写点 {pair['分支ID']}　{pair['源码位置']}\n{pair['收录依据']}"
        for ref in info.get('witnesses', ()):
            obs += (f"\n关联实测 {ref['start']} → {ref['end']}，采集 {ref['recording']}，样本 {ref['sample_index']}"
                    + (f"，判断 {ref['decision']}" if ref.get('decision') else ''))
    if count := pair.get('发生次数'):
        obs += f'，出现 {count} 次'
    if pair.get('有效计时次数') not in (None, ''):
        obs += f'，完整计时 {pair["有效计时次数"]} 次'
        for field, reason in [('采集首段未计时次数', '采集开始'), ('缺口后未计时次数', '恢复采样')]:
            if int(pair.get(field) or 0):
                obs += f'，{reason}时已在本步 {pair[field]} 次'
    duration = pair.get('中位数时长ms')
    if duration not in (None, ''):
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
    if path := info.get('candidate_path'):
        steps = [path[0]['from'], *(edge['to'] for edge in path)]
        parts.append('源码候选路径（中间步未经本次采样确认） ' + ' → '.join(map(str, steps)))
    if variables := desc.get('动作变量') or pair.get('动作变量'):
        parts.append('源码动作变量（引用） ' + variables)
    detail = desc.get('证据')
    if detail:
        parts.append(detail)
    elif desc.get('动作描述'):
        parts.append('描述依据 ' + (desc.get('证据引用') or '定位信息未提供，待核'))
    else:
        parts.append('动作描述待核：' + (desc.get('待核原因') or desc.get('描述状态') or '缺少已确认的描述依据'))
    if reasoning := desc.get('推断依据'):
        parts.append((desc.get('证据等级') or '描述推断依据') + '：' + reasoning)
    if supplement := desc.get('注释补充'):
        parts.append('原注补充依据：' + supplement)
    if status := desc.get('描述状态'):
        parts.append('描述状态：' + status)
    annotations = [item for item in pair.get('备注', '').split(' | ') if item and item != relation]
    if annotations:
        parts.append('状态对记录：' + '；'.join(annotations))
    if role := desc.get('等待判定'):
        parts.append(f'{role}依据：{desc["等待依据"]}')
    return '\n'.join(parts)


def _channel_sort(name):
    return tuple(int(p) if i % 2 else p for i, p in enumerate(re.split(r'([+-]?\d+)', name.casefold()))), name


def _scope_owner(record, native):
    """Use the outer call site's program/action, or a Sysmac program/section pair."""
    scope = record.get('scope', '')
    if calls := record.get('instance_path'):
        scope = calls[0].get('scope', '')
    program = record.get('program', '')
    if program:
        if scope == program:
            return program, ''
        if scope.startswith(program + '::'):
            return program, scope[len(program) + 2:].split('::', 1)[0]
        return None
    if record.get('source_file') == 'indexed_candidates.csv' and ';' in scope:
        return None  # This inventory can aggregate several source locations.
    if native and scope and '<anonymous>' not in scope:
        # The Sysmac ledger constructs this path from Program and BodyContent
        # ancestors. Other exporters can use the same separator for folders.
        parts = scope.split('::')
        if all(parts):
            return parts[0], '::'.join(parts[1:])
    return None


def _row_owners(info, native_source):
    """Return the starting step's owner and, independently, its selected annotation's owner."""
    if info.get('relation') == 'ambiguous_scope':
        return None, None
    refs = set(info.get('description_refs', ()))
    direct = info.get('direct', ())
    annotations = [n for n in info.get('start_notes', ()) if n.get('id') in refs]
    annotations.extend(e for e in direct if e.get('source_id') in refs)
    # A program that only reads the state must not displace its writer. If
    # competing writers are found at the same level, keep the owner unresolved.
    tiers = (direct or info.get('candidate_path', ())[:1], info.get('start_state_writes', ()),
             info.get('start_commands', ()), annotations)
    records = [record for tier in tiers for record in tier]
    native = native_source or any(r.get('grade', '').startswith('omron_native_') or
                                  r.get('source_file') == 'native_command_evidence.csv' for r in records)
    owners_by_scope = defaultdict(set)
    for record in records:
        if owner := _scope_owner(record, native):
            owners_by_scope[record['scope']].add(owner)

    def unique(tier):
        choices = [owners_by_scope[record.get('scope', '')] for record in tier]
        if choices and all(choices):
            owners = set.union(*choices)
            if len(owners) == 1:
                return owners.pop()
        return None

    return unique(next((tier for tier in tiers if tier), ())), unique(annotations)


def _component_blocks(groups, row_owners):
    """Split only at a channel or ownership change; retain the existing order of observed pairs."""
    station_blocks = []
    components = {}
    for station, rows in groups:
        blocks = []
        channels = sorted(rows.groupby('_步序变量', sort=False), key=lambda group: _channel_sort(group[0]))
        for channel, records in channels:
            pairs = records.fillna('').to_dict('records')
            for owner, batch in groupby(pairs, key=lambda p: row_owners.get(p.get('分支ID') or (p['开始标签'], p['结束标签']))):
                blocks.append((channel, owner, list(batch)))
        station_blocks.append((station, blocks))
        for channel, owner, _ in blocks:
            label = station if len(blocks) == 1 else f'{station}\n{channel}'
            scope = ''
            if owner:
                program, routine = owner
                scope = program
                if routine:
                    scope += '::' + routine
                    name = program.rsplit('::', 1)[-1]
                    # Keep namespace and numeric substation segments in the code.
                    code = re.match(r'^((?:[A-Za-z][A-Za-z0-9]*_)*[A-Za-z]+\d+(?:_\d+)*)(?=$|[_ -])', name)
                    label = f'{code[1] if code else name}-{routine}'
            components[station, channel, owner] = (label, scope)
    counts = Counter(label for label, _ in components.values())
    channel_counts = Counter((label, channel) for (_, channel, _), (label, _) in components.items())
    for key, (label, scope) in components.items():
        station, channel, _ = key
        if counts[label] > 1:
            identifier = (scope or station) if channel_counts[label, channel] > 1 else label
            components[key] = (f'{identifier}\n{channel}', scope)
    return station_blocks, components


def _remark(pair, desc, info, owner, role):
    notes = []
    if supplement := desc.get('注释补充'):
        if owner and supplement != '例程归属待核':
            program, routine = owner
            notes.append(f'这条注释指「{routine or program.rsplit("::", 1)[-1]}」流程。')
        else:
            notes.append('这条注释所属的流程仍待确认。')
    relation = pair.get('源码关系')
    if relation in ('path_candidate', 'path_via_zero_candidate'):
        via = [str(edge['to']) for edge in info.get('candidate_path', ())[:-1]]
        if via:
            steps = '步 ' + '、'.join(via) if len(via) <= 3 else f'{len(via)} 个中间步'
            notes.append(f'两次采样间可能经过{steps}。')
        else:
            notes.append('两次采样间可能经过其他步。')
    elif relation == 'direct_unverified':
        notes.append('进入结束步的条件尚未核实。')
    elif relation == 'ambiguous_scope':
        notes.append('多个源码位置都可能控制本步，需确认归属。')
    elif relation == 'unresolved':
        notes.append('源码中尚未找到与这次跳变对应的路径。')
    if not desc.get('动作描述'):
        status = desc.get('描述状态')
        notes.append({'B档待处理': '动作描述待补写。', 'B档待修订': '动作描述需重新核对。'}.get(
            status, '动作描述缺少可核对的依据。'))
    if role in ('待料', '堵料'):
        direction = '上游供料' if role == '待料' else '下游接料'
        notes.append(f'等待{direction}。' if not desc.get('描述来源', '').startswith('B档')
                     else f'推断为等待{direction}，请核对。')
    if str(pair.get('有效计时次数')) == '0' and pair.get('本次状态') != '本次未观测':
        notes.append('进入本步的时刻未采到，时长留空。')
    if int(pair.get('缺口后未计时次数') or 0):
        notes.append(f'恢复采样后有 {pair["缺口后未计时次数"]} 次仅计跳变，见「采样缺口」。')
    if pair.get('估算时长次数') not in (None, '', 0, '0'):
        notes.append(f'有 {pair["估算时长次数"]} 次停留时长为估算值。')
    return ''.join(notes) or None


def restore_annotation_details(evidence, descriptions):
    """Re-render a saved A-only or older run from its own selected annotations."""
    for key, info in evidence.items():
        text = info.get('description')
        if not text or not info.get('description_source', '').startswith('A档'):
            continue
        start, end = key.split('|||', 1)
        row = descriptions.setdefault((start, end), {})
        if row.get('动作描述') not in (None, '', text) or row.get('描述来源', '').startswith('B档'):
            continue
        role, basis = waiting_role(info, start)
        row.update({'动作描述': text, '描述来源': info['description_source'], '描述状态': '源码注释',
                    '证据': annotation_evidence_text(info), '注释补充': annotation_supplement(info),
                    '等待判定': role, '等待依据': basis})


def _write_sampling_gaps(book, run):
    path = Path(run) / GAP_FILE
    if not path.is_file():
        return  # Re-rendering an older run.
    with path.open(encoding='utf-8') as stream:
        records = json.load(stream)['recordings']
    if not any(r['common_gaps'] or r['channel_gaps'] for r in records):
        return
    ws = book.create_sheet('采样缺口')
    ws.append(['时间为各次采集内的毫秒。两端有样本，中间无连续观测，端点值仅供核对。'])
    ws.merge_cells('A1:K1')
    ws.append(['范围', '采集ID', '通道', '前样本索引', '前样本时间(ms)', '前值', '后样本索引',
               '后样本时间(ms)', '后值', '间隔(ms)', '判据'])
    for record in records:
        rid = record['recording_id']
        for gap in record['common_gaps']:
            ws.append(['全部通道', rid, None, None, gap['left_ms'], None, None, gap['right_ms'],
                       None, gap['duration_ms'], '所有通道均缺少样本的区间'])
        for gap in record['channel_gaps']:
            ws.append(['单通道', rid, gap['channel'], gap['left_index'], gap['left_ms'], gap['left_value'],
                       gap['right_index'], gap['right_ms'], gap['right_value'],
                       round(gap['right_ms'] - gap['left_ms'], 6), gap['reason']])
    for col, width in enumerate([14, 20, 38, 15, 21, 12, 15, 21, 12, 18, 40], 1):
        ws.column_dimensions[get_column_letter(col)].width = width
        cell = ws.cell(2, col)
        cell.fill, cell.font, cell.alignment = FILLS['表头'], BOLD, CENTER
    ws.freeze_panes = 'D3'
    ws.auto_filter.ref = f'A2:K{ws.max_row}'


def _observation_sheets(book, run):
    path = Path(run) / OBSERVATIONS_FILE
    candidates = json.loads((Path(run) / BRANCH_EVIDENCE).read_text(encoding='utf-8'))['candidates']
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    fields = ('构成要素', '开始标签', '结束标签', '发生次数', '有效计时次数', '中位数时长ms',
              '分支判定', '样例采集ID', '样例跳变索引', '分支ID')
    for title, selected, headers in (
            ('实测跳变', rows, (*fields, '跳变来源', '源码步序路径', '所属程序段', '其他写入来源')),
            ('分支待核', (r for r in rows if r['分支判定'] != '分支已观测' or r['待核出口']),
             (*fields[:7], '跳变来源','源码步序路径','所属程序段','候选写点及位置', '待核出口', '工况核对信号','其他写入来源'))):
        ws = book.create_sheet(title)
        for col, name in enumerate(headers, 1):
            c = _cell(ws, 1, col, name)
            c.fill = FILLS['表头']
            ws.column_dimensions[c.column_letter].width = 68 if name in ('候选写点及位置','源码步序路径','其他写入来源','所属程序段') else 30 if col in (2, 3, 7) else 21
        for i, row in enumerate(selected, 2):
            for col, name in enumerate(headers, 1):
                value = row.get(name, '')
                if name == '候选写点及位置':
                    items = (candidates[ident] for ident in row['候选写点'].split('、') if ident)
                    value = '\n'.join(filter(None, [*(f"{e['from']} → {e['to']}　{e['location']}" for e in items), row.get('待核写入')]))
                elif name in ('发生次数', '有效计时次数', '中位数时长ms', '样例跳变索引') and value:
                    value = float(value)
                cell = _cell(ws, i, col, value if value != '' else None)
                if name in ('候选写点及位置','其他写入来源','源码步序路径','所属程序段'):
                    cell.alignment = Alignment(vertical='top', wrap_text=True)
        ws.freeze_panes = 'D2'
        ws.auto_filter.ref = ws.dimensions


def write_breakdown(groups, descriptions, out, proc, digest, run, evidence, *, branch_mode=False):
    book = Workbook()
    sheet = book.active
    _header(sheet, proc, digest)
    columns = COLUMNS
    if branch_mode:
        columns += (('本次状态', 27), ('转移条件', 52), ('关联实测跳变', 30), ('源码位置', 52), ('适用工况', 45))
        for col, (heading, width) in enumerate(columns[len(COLUMNS):], len(COLUMNS) + 1):
            cell = _cell(sheet, 2, col, heading)
            cell.fill = FILLS['表头']
            sheet.column_dimensions[cell.column_letter].width = width
    audits = {name for name in ('trace_jumps.csv', 'trace_segments.csv', EVIDENCE_FILE) if (Path(run) / name).is_file()}
    manifest = Path(run) / 'workflow_manifest.json'
    native_source = False
    if evidence and manifest.is_file():
        with manifest.open(encoding='utf-8-sig') as stream:
            native_source = json.load(stream).get('extraction', {}).get('format') == 'omron_iec61131_10'
    owners, annotation_owners = {}, {}
    for key, info in evidence.items():
        pair_key = key if branch_mode else tuple(key.split('|||', 1))
        owners[pair_key], annotation_owners[pair_key] = _row_owners(info, native_source)
    station_blocks, components = _component_blocks(groups, owners)
    review_rows = b_rows = measured = 0
    waits = Counter()
    r = 3
    for station, blocks in station_blocks:
        total = None
        missing_times = 0
        for idx, (channel, owner, records) in enumerate(blocks):
            if idx and channel != blocks[idx - 1][0]:
                for gap in (r, r + 1):
                    sheet.row_dimensions[gap].height = 10
                r += 2
            first = r
            for pair in records:
                start, end = pair['开始标签'], pair['结束标签']
                key = pair.get('分支ID') or (start, end)
                desc = descriptions.get(key, {})
                info = evidence.get(key if branch_mode else f'{start}|||{end}', {})
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
                duration = pair.get('中位数时长ms', '')
                ms = float(duration) if duration != '' else None
                if ms is not None:
                    total = (total or 0) + ms
                    measured += 1
                else:
                    missing_times += 1
                values = {4: text or None, 5: origin or None, 6: ms, 8: start, 9: end,
                          13: 1 if role else None,
                          14: _remark(pair, desc, info, annotation_owners.get(key), role),
                          15: _evidence(pair, desc, info, audits)}
                if branch_mode:
                    values.update({16: pair['本次状态'], 17: pair['原始条件'], 18: pair['关联实测'], 19: pair['源码位置'], 20: pair['适用工况']})
                for col, (heading, width) in enumerate(columns, 1):
                    value = values.get(col)
                    cell = _cell(sheet, r, col, value)
                    if col >= 4 and role in ('待料', '堵料'):
                        cell.fill = FILLS[role]
                    if col in (2, 3, 6, 7):
                        cell.number_format = '0.00##'
                    if col in (4, 14, 15, 17, 18, 19, 20):
                        cell.alignment = LEFT
                    if col == 5 and origin == 'LLM':
                        cell.font = RED
                    if isinstance(value, str) and ('\n' in value or _width(value) > width - 1):
                        _details(book, cell, start, end, heading, value)
                sheet.row_dimensions[r].height = STEP_HEIGHT
                r += 1
            # For an unsplit station the template's merged group includes its total.
            last = r if len(blocks) == 1 else r - 1
            name, scope = components[station, channel, owner]
            label = _cell(sheet, first, 1, name)
            label.fill, label.font, label.alignment = FILLS['构成要素'], BOLD, GROUP
            basis = f'源码位置：{scope}' if scope else '这些步序的源码例程尚未确定，保留工位分组。'
            label.comment = Comment(f'工位：{station}\n完整通道：{channel}\n{basis}', 'PLC Trace')
            if last > first:
                sheet.merge_cells(start_row=first, end_row=last, start_column=1, end_column=1)
            needed = sum(max(1, ceil(_width(line) / (COLUMNS[0][1] - 1))) for line in name.splitlines())
            if needed > last - first + 1:
                _details(book, label, records[0]['开始标签'], records[0]['结束标签'], '构成要素', label.value)
        subtotal = '本工位各行停留中位数之和，不能作为单次循环时间。'
        if missing_times:
            subtotal = f'有 {missing_times} 行缺少完整计时，小计仅含有时长的行。'
        values = {4: '总时长', 6: round(total, 4) if total is not None else None, 14: subtotal}
        if branch_mode:
            values = {4: '工位分支汇总', 14: '各行对应不同工况，时长按分支分别统计。'}
        for col in range(2, len(columns) + 1):
            cell = _cell(sheet, r, col, values.get(col))
            cell.font = BOLD
            if col == 6:
                cell.number_format = '0.00##'
            if col in (4, 14):
                cell.alignment = LEFT
        if len(blocks) > 1:
            label = _cell(sheet, r, 1, station)
            label.fill, label.font = FILLS['构成要素'], BOLD
        sheet.row_dimensions[r].height = 13.8
        r += 1
    if branch_mode:
        _observation_sheets(book, run)
    _write_sampling_gaps(book, run)
    book.save(out)
    return {'trace_review_rows': review_rows, 'b_desc_rows': b_rows,
            'trace_duration_backfilled': measured > 0, 'wait_rows': dict(waits)}
