# -*- coding: utf-8 -*-
"""Shared utilities for PLC agent modules.

Extracted in v9.28 to eliminate duplication across gen_stepvars_xlsx.py,
gen_workstation_xlsx.py, and trace_to_pairs.py.

Contents:
  - load_station_identity(run)  — load station_identity.json (3 callers)
  - infer_proc_name(source, run, proc_name)  — infer process name (2 callers)
  - em_sort_key_prefixed(name)  — sort key with prefix preserved (2 callers)

Note: gen_workstation_xlsx._em_sort_key (B version, EM/ST-only numeric) and
gen_workstation_xlsx.load_var_docs (clean_doc version) are intentionally NOT
merged here — they have different semantics from their namesakes.
"""
import json
import os
import re


def load_station_identity(run: str) -> dict:
    """Load station_identity.json (var_root -> {display, code, sort_key, ...}).

    Returns {} when file is absent or run is None.  Shared by
    gen_stepvars_xlsx, gen_workstation_xlsx, and trace_to_pairs.
    """
    if not run:
        return {}
    p = os.path.join(run, 'station_identity.json')
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding='utf-8'))
        except Exception:
            return {}
    return {}


def infer_proc_name(source: str, run: str = None, proc_name: str = None) -> str:
    """Infer process name from source filename, run dir name, or explicit override.

    Priority: proc_name > source filename (last Chinese segment) > run dir name.
    Strips _YYYYMMDD and _vN suffixes.
    """
    if proc_name:
        return proc_name
    base = ''
    if source:
        base = os.path.basename(source)
        base = re.sub(r'\.(xml|smc2|txt)$', '', base, flags=re.I)
        base = re.sub(r'_\d{8}$', '', base)
        base = re.sub(r'_v\d+$', '', base, flags=re.I)
        segs = re.findall(r'[\u4e00-\u9fff]+', base)
        if segs:
            base = segs[-1]
    if not base and run:
        base = os.path.basename(os.path.normpath(run))
        base = re.sub(r'_v\d+$', '', base, flags=re.I)
    return base or '工序'


def em_sort_key_prefixed(name: str):
    """Sort key so EM01 < EM02 < ... < EM10, EM07_1 < EM07_2.

    Preserves the alphabetic prefix in the sort key (unlike the EM/ST-only
    version in gen_workstation_xlsx).  Returns (prefix_str, [main_num, sub_num])
    so names with different prefixes sort by prefix first, then by number.
    Used by gen_stepvars_xlsx and trace_to_pairs.
    """
    m = re.match(r'^([A-Za-z]*)(\d+)(?:_(\d+))?', str(name))
    if not m:
        return (str(name), [0, 0])
    return (m.group(1) or '', [int(m.group(2)),
                                int(m.group(3)) if m.group(3) else 0])
