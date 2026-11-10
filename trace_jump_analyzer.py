"""Read TwinCAT Scope captures and identify within-capture cycle boundaries."""
import csv
import re
import sys


def parse_trace(trace_path):
    """Parse a TwinCAT Scope View .trace XML (UTF-16) file.

    Returns dict: var_name -> {
        'values': [float,...], 'timestamps': [float,...],
        'jumps': [(idx, from_val, to_val, ts_ms), ...],
        'segments': [(value, start_ts, end_ts, duration_ms), ...],
        'sample_count': int, 'time_range': (first_ts, last_ts),
        'inactive': bool, 'unique_values': [float,...]
    }
    """
    with open(trace_path, 'r', encoding='utf-16') as f:
        txt = f.read()

    # Locate each <TraceVariable>...</TraceVariable> block.
    starts = [m.start() for m in re.finditer(r'<TraceVariable\b', txt)]
    ends = [m.start() for m in re.finditer(r'</TraceVariable>', txt)]
    if len(starts) != len(ends):
        raise ValueError(f'TraceVariable 开始/结束标签数不匹配: {len(starts)} vs {len(ends)}')

    variables = {}
    for i in range(len(starts)):
        block = txt[starts[i]:ends[i] + len('</TraceVariable>')]
        name_m = re.search(r'VarName="([^"]+)"', block)
        if not name_m:
            continue
        name = name_m.group(1)
        vals_m = re.search(r'<Values>(.*?)</Values>', block, re.DOTALL)
        ts_m = re.search(r'<Timestamps>(.*?)</Timestamps>', block, re.DOTALL)
        if not vals_m or not ts_m:
            continue
        try:
            values = [float(v) for v in vals_m.group(1).split(',') if v.strip()]
            timestamps = [float(t) for t in ts_m.group(1).split(',') if t.strip()]
        except ValueError as e:
            print(f'trace 变量 {name} 数值解析失败: {e}', file=sys.stderr)
            continue
        if len(values) != len(timestamps):
            print(f'trace 变量 {name} 值数({len(values)})≠时间戳数({len(timestamps)})，跳过', file=sys.stderr)
            continue
        # jumps
        jumps = []
        for j in range(1, len(values)):
            if values[j] != values[j - 1]:
                jumps.append((j, values[j - 1], values[j], timestamps[j]))
        # segments
        segments = []
        seg_start = 0
        for j in range(1, len(values)):
            if values[j] != values[j - 1]:
                segments.append((values[seg_start], timestamps[seg_start], timestamps[j],
                                 timestamps[j] - timestamps[seg_start]))
                seg_start = j
        if seg_start < len(values):
            segments.append((values[seg_start], timestamps[seg_start], timestamps[-1],
                             timestamps[-1] - timestamps[seg_start]))
        unique_vals = sorted(set(values))
        inactive = (len(jumps) == 0)
        variables[name] = {
            'values': values,
            'timestamps': timestamps,
            'jumps': jumps,
            'segments': segments,
            'sample_count': len(values),
            'time_range': (timestamps[0] if timestamps else 0, timestamps[-1] if timestamps else 0),
            'inactive': inactive,
            'unique_values': unique_vals,
        }
    return variables


# ---------- Cycle detection ----------

def detect_cycles(variables, idle_threshold_ms=5000):
    """Detect production cycle boundaries from long idle (value=0) segments.

    Excludes variables that never have value 0 (continuous runners like EM13V).
    Returns list of (boundary_ts, var_name) sorted by ts.
    """
    boundaries = []
    for name, v in variables.items():
        if v['inactive']:
            continue
        for value, s_ts, e_ts, dur in v['segments']:
            if value == 0.0 and dur >= idle_threshold_ms:
                boundaries.append((e_ts, name))  # boundary = end of idle
    boundaries.sort()
    return boundaries


def _write_csv(path, rows, fields):
    with open(path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
