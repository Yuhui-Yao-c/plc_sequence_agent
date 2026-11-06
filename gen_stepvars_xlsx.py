# -*- coding: utf-8 -*-
"""Render the step-variable inventory workbook from indexed_candidates.csv.

v9.26: instead of re-parsing the XML, we reuse the candidates that
predict already produced.  Every row in indexed_candidates.csv has
开始标签 / 结束标签 columns of the form  udt_EM01.adiAutoStep[5]=150.
Stripping the trailing =<number> yields the step-variable symbol:
  udt_EM01.adiAutoStep[5]

This reuses the full parsing infrastructure (agent.py + plcopen_controlflow
+ omron_native + smc2) — the same machinery that finds start/end labels
also finds every step-variable symbol.  No separate XML scan is needed.

Output: <proc_name>_步序变量表.xlsx  (2 cols: 工位名 | 步序变量)
        step_variables.json / step_variables.csv (audit detail)

Usage:
  python gen_stepvars_xlsx.py --run outputs/<run> [--source XML] [--proc-name 工序名] [--out path]
  (importable from plc_cli.predict --tagvars path)
"""
import argparse, csv, json, os, re
from collections import OrderedDict
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from utils import load_station_identity, infer_proc_name, em_sort_key_prefixed


# Capture  var[idx]  from a label like  udt_EM01.adiAutoStep[5]=150  or
# NOT udt_EM01.adiAutoStep[5]=150.  We keep the full dotted prefix so
# udt_EM01.adiAutoStep[5] is preserved (not stripped to EM01).
_SYMBOL_RE = re.compile(
    r'([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\[\d+\])'
)


def _strip_v(name: str) -> str:
    """EM01V -> EM01.  Only strips a single trailing V after a digit."""
    if name.endswith('V') and len(name) > 1 and name[-2].isdigit():
        return name[:-1]
    return name


def _station_name(instance_root: str, station_identity: dict,
                  var_docs: dict) -> str:
    """Resolve display station name; fallback strips trailing V."""
    si = station_identity.get(instance_root)
    if si and si.get('display'):
        return si['display']
    doc = var_docs.get(instance_root)
    if doc:
        return f'{_strip_v(instance_root)}_{doc}'
    return _strip_v(instance_root)


def _load_var_docs(run: str) -> dict:
    p = os.path.join(run, 'variable_documentation.json')
    if not os.path.exists(p):
        return {}
    try:
        data = json.load(open(p, encoding='utf-8'))
        st = data.get('stations') or {}
        src = st if st else data.get('all', {})
        out = {}
        for k, v in src.items():
            t = re.sub(r'<[^>]+>', '', str(v)).strip()
            if t:
                out[k] = t
        return out
    except Exception:
        return {}


def extract_symbols_from_candidates(run: str) -> list:
    """Read indexed_candidates.csv and extract unique step-variable symbols.

    Returns a list of dicts: [{工位名, 步序变量, 实例, 变量, 下标, 出现次数}, ...]
    """
    cand_path = os.path.join(run, 'indexed_candidates.csv')
    if not os.path.exists(cand_path):
        # fallback to prediction_candidate_pairs.csv
        cand_path = os.path.join(run, 'prediction_candidate_pairs.csv')
    if not os.path.exists(cand_path):
        raise FileNotFoundError(
            f'缺少候选文件 indexed_candidates.csv（请先运行 predict 生成候选）')

    station_identity = load_station_identity(run)
    var_docs = _load_var_docs(run)

    # symbol -> {instance_root, var, idx, count}
    symbols = OrderedDict()
    label_cols = ('开始标签', '结束标签', '原始开始标签', '原始结束标签')

    with open(cand_path, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            for col in label_cols:
                val = row.get(col, '')
                if not val:
                    continue
                for m in _SYMBOL_RE.finditer(val):
                    symbol = m.group(1)
                    if symbol not in symbols:
                        # parse instance.var[idx]
                        # instance = everything before last .var[
                        pm = re.match(
                            r'^(.+)\.([A-Za-z_]\w*)\[(\d+)\]$', symbol)
                        if not pm:
                            continue
                        instance = pm.group(1)
                        var = pm.group(2)
                        idx = int(pm.group(3))
                        instance_root = instance.split('.', 1)[0]
                        symbols[symbol] = {
                            '工位名': _station_name(instance_root,
                                                    station_identity,
                                                    var_docs),
                            '步序变量': symbol,
                            '实例': instance,
                            '变量': var,
                            '下标': idx,
                            '出现次数': 0,
                        }
                    symbols[symbol]['出现次数'] += 1

    rows = list(symbols.values())
    rows.sort(key=lambda r: (em_sort_key_prefixed(r['工位名']),
                             em_sort_key_prefixed(r['步序变量']),
                             r['下标']))
    return rows


def generate(run: str, source: str = None, proc_name: str = None,
             out: str = None) -> dict:
    """Generate <proc_name>_步序变量表.xlsx + audit files from run candidates."""
    rows = extract_symbols_from_candidates(run)

    proc = infer_proc_name(source, run, proc_name)
    if not out:
        out = os.path.join(run, f'{proc}_步序变量表.xlsx')

    # Write audit JSON + CSV
    json_path = os.path.join(run, 'step_variables.json')
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    csv_path = os.path.join(run, 'step_variables.csv')
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['工位名', '步序变量', '实例',
                                          '变量', '下标', '出现次数'])
        w.writeheader()
        w.writerows(rows)

    # Render xlsx
    wb = Workbook()
    ws = wb.active
    ws.title = '步序变量表'

    hfont = Font(name='Arial', size=9, bold=True)
    bfont = Font(name='Arial', size=9)
    hfill = PatternFill('solid', fgColor='E8E8E8')
    left = Alignment(horizontal='left', vertical='center', wrap_text=False)
    center = Alignment(horizontal='center', vertical='center', wrap_text=False)
    thin = Side(style='thin', color='BFBFBF')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Header note row
    note = (f'工序: {proc} | 来源: {os.path.basename(source) if source else ""} '
            f'| 变量数: {len(rows)} | 生成: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    ws.cell(row=1, column=1, value=note).font = Font(
        name='Arial', size=8, italic=True, color='808080')
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=2)
    ws.cell(row=1, column=1).alignment = left

    # Title row
    ws.cell(row=2, column=1, value=f'{proc} 步序变量表').font = Font(
        name='Arial', size=11, bold=True)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=2)
    ws.cell(row=2, column=1).alignment = center

    # Column header row
    for c, h in enumerate(['工位名', '步序变量'], 1):
        cell = ws.cell(row=3, column=c, value=h)
        cell.font = hfont
        cell.fill = hfill
        cell.alignment = center
        cell.border = border

    # Data rows
    start_row = 4
    for i, r in enumerate(rows):
        rr = start_row + i
        ws.cell(row=rr, column=1, value=r['工位名']).font = bfont
        ws.cell(row=rr, column=1).alignment = left
        ws.cell(row=rr, column=1).border = border
        ws.cell(row=rr, column=2, value=r['步序变量']).font = bfont
        ws.cell(row=rr, column=2).alignment = left
        ws.cell(row=rr, column=2).border = border
        ws.row_dimensions[rr].height = 18

    # Vertical merge for workstation column
    if rows:
        i = 0
        while i < len(rows):
            j = i
            name = rows[i]['工位名']
            while j + 1 < len(rows) and rows[j + 1]['工位名'] == name:
                j += 1
            if j > i:
                ws.merge_cells(start_row=start_row + i, start_column=1,
                               end_row=start_row + j, end_column=1)
                ws.cell(row=start_row + i, column=1).alignment = Alignment(
                    horizontal='left', vertical='center', wrap_text=False)
            i = j + 1

    ws.column_dimensions['A'].width = 30
    ws.column_dimensions['B'].width = 42
    ws.row_dimensions[1].height = 16
    ws.row_dimensions[2].height = 24
    ws.row_dimensions[3].height = 20
    ws.freeze_panes = 'A4'

    wb.save(out)
    return {
        'proc_name': proc,
        'rows': len(rows),
        'stations': len(set(r['工位名'] for r in rows)),
        'out': out,
        'json': json_path,
        'csv': csv_path,
    }


def main():
    ap = argparse.ArgumentParser(description='从候选CSV提取步序变量并渲染xlsx')
    ap.add_argument('--run', required=True, help='run目录（含indexed_candidates.csv）')
    ap.add_argument('--source', default=None, help='源码XML（推断工序名用）')
    ap.add_argument('--proc-name', default=None, help='工序名')
    ap.add_argument('--out', default=None, help='输出xlsx路径')
    a = ap.parse_args()
    stats = generate(a.run, a.source, a.proc_name, a.out)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
