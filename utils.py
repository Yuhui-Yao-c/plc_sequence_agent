"""Shared station, naming and sort helpers for Trace."""
import json
import re
from pathlib import Path, PurePosixPath


def load_station_identity(run):
    """Load the source-backed station names when available."""
    if not run:
        return {}
    path = Path(run) / 'station_identity.json'
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    except (OSError, ValueError):
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
    Used for Trace state pairs.
    """
    m = re.match(r'^([A-Za-z]*)(\d+)(?:_(\d+))?', str(name))
    if not m:
        return (str(name), [0, 0])
    return (m.group(1) or '', [int(m.group(2)),
                                int(m.group(3)) if m.group(3) else 0])
