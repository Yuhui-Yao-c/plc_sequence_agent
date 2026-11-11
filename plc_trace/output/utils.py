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
from pathlib import PurePosixPath


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
    """Prefer the named source folder, then meaningful names, never 'PLC程序'."""
    if proc_name:
        name = str(proc_name).strip()
        if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name in ('.', '..'):
            raise ValueError('工序名称须为有效文件名，不能包含路径分隔符')
        return name
    generic = {'source', 'sources', 'src', 'input', 'inputs', 'output', 'outputs',
               'data', 'project', 'repo', '工程', '源码', '程序', '工序', 'xml', 'plc'}
    def clean(name):
        name = re.sub(r'\.(xml|smc2|txt)$', '', name, flags=re.I)
        name = re.sub(r'(?:[_ -]?\d{8,14})$', '', name)
        name = re.sub(r'[_ -]v\d+(?:\.\d+)*$', '', name, flags=re.I)
        return re.sub(r'[_ -]trace$', '', name, flags=re.I).strip(' _-.')
    source_name = ''
    if source:
        path = PurePosixPath(str(source).replace('\\', '/'))
        parent = clean(path.parent.name)
        if parent and parent.casefold() not in generic and (
                re.search(r'[\u4e00-\u9fff]', parent) or path.parent.parent.name.casefold() in generic):
            return parent
        source_name = clean(path.name)
        meaningful = re.sub(r'(?:PLC\s*)?(?:程序|工程|源码)|PLC', '', source_name, flags=re.I).strip(' _-.')
        parts = [s for s in re.split(r'[_\s]+', meaningful) if re.search(r'[\u4e00-\u9fff]', s)]
        if parts:
            return parts[-1]
        segs = [s for s in re.findall(r'[\u4e00-\u9fff]+', source_name)
                if s.casefold() not in generic and s not in ('程序', '工程', '源码')]
        if segs:
            return segs[-1]
    if run:
        name = clean(PurePosixPath(str(run).replace('\\', '/')).name)
        if name and name.casefold() not in generic:
            return name
    source_name = re.sub(r'(?:PLC\s*)?(?:程序|工程|源码)|PLC', '', source_name, flags=re.I).strip(' _-.')
    return source_name or '工序'


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
