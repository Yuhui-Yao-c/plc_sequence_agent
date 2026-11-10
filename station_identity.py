"""Resolve Trace workstation names from observed variables and XML documentation."""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


_TYPE_PREFIX = re.compile(r'^(?:udt_|[sS][tT])(?=[A-Z])')


def _clean_doc(text: str) -> str:
    """Documentation may wrap a GrpComment XML fragment; keep the plain zh name."""
    t = str(text)
    m = re.search(r'<GrpComment_zh-cn>(.*?)</GrpComment_zh-cn>', t, re.S)
    if m:
        t = m.group(1)
    i = t.find('<Sub/>')
    if i >= 0:
        t = t[:i]
    t = re.sub(r'<[^>]+>', '', t)
    return re.sub(r'\s+', '', t).strip()


def _display_code(var_root: str) -> str:
    """Extract the display code by stripping ONLY standard type prefixes.

    udt_EM04 -> EM04; stEM310 -> EM310; HJXHX_ST08 -> HJXHX_ST08 (preserved);
    ST06 -> ST06 (no prefix to strip); '' -> ''.
    """
    s = str(var_root)
    if not s.strip() or s.lower() in ('nan', 'none'):
        return ''
    return _TYPE_PREFIX.sub('', s)


def _sort_key(var_root: str):
    """Numeric sort key extracted from the LAST number group in the root.

    EM01 -> (1,0); EM07_1 -> (7,1); HJXHX_ST08 -> (8,0); ST06 -> (6,0).
    Non-numeric roots sort last stably.  Uses the last group so HJXHX_ST08
    (8) < HJXHX_ST10 (10), not (0,) < (0,) by the HJXHX prefix.
    """
    s = str(var_root)
    groups = re.findall(r'(\d+)(?:_(\d+))?', s)
    if not groups:
        return (10 ** 9, 0)
    last = groups[-1]
    return (int(last[0]), int(last[1]) if last[1] else 0)


def _pou_area_to_station(area: str):
    """Extract (code, name) from a POU area like 'EM02_电芯上料_滚轮线_L02_L04_L80_L86'.

    Returns (code, name) or (None, None) if not an EM-type POU area.
    code = 'EM02', name = '电芯上料_滚轮线' (strips _Lxx suffixes).
    """
    m = re.match(r'(EM\d+)_(.+?)(?:_L\d+.*)?$', area)
    if m:
        code = m.group(1)
        name = m.group(2)
        name = re.sub(r'_L\d+.*$', '', name)
        return code, name
    return None, None


def source_station_displays(groups, identity):
    """Use the documented name when one qualified source scope confirms it.

    Each input is (display, controller, source_scopes). Keep the existing
    label if sources disagree, are missing, or a rename would merge groups.
    """
    if not identity:
        return {}
    displays, blocked = set(), set()
    docs, owners = defaultdict(set), defaultdict(set)
    for display, controller, scopes in groups:
        displays.add(display)
        root = controller.split('.', 1)[0]
        entry = identity.get(controller) or identity.get(root, {})
        doc, code = entry.get('doc'), entry.get('code')
        if not doc or not code or display != f'{code}_{doc}':
            blocked.add(display)
            continue
        docs[display].add(doc)
        for scope in scopes.split(';'):
            if not scope.strip():
                continue
            parts = tuple(part.strip() for part in scope.split('::'))
            matches = [index for index, part in enumerate(parts) if _clean_doc(part) == doc]
            if len(matches) != 1:
                blocked.add(display)
                continue
            owners[display].add(parts[:matches[0] + 1])

    names = {display: next(iter(labels)) for display, labels in docs.items()
             if display not in blocked and len(labels) == len(owners[display]) == 1}
    counts = Counter(names.values())
    return {display: name for display, name in names.items()
            if name not in displays and counts[name] == 1}


def build_identity(run, variables, scopes):
    path = Path(run) / 'variable_documentation.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    docs = {**(data.get('all') or {}), **(data.get('stations') or {})}
    identity = {}
    roots = {var.split('.', 1)[0].split('[', 1)[0] for var in variables}
    for root in sorted(roots):
        code = _display_code(root)
        doc = _clean_doc(docs.get(root, ''))
        identity[root] = {
            'display': f'{code}_{doc}' if doc and code else (doc or code or root),
            'code': code, 'doc': doc, 'sort_key': list(_sort_key(root)),
            'source': 'variable_documentation' if doc else 'none',
            'needs_review': not doc,
        }
    # Use the scope of the exact indexed channel, including qualified members.
    for variable in variables:
        if not re.search(r'\[\d+\]$', variable):
            continue
        counts = scopes.get(variable.casefold())
        if not counts:
            continue
        code, doc = _pou_area_to_station(max(counts, key=counts.get))
        if code:
            identity[variable] = {
                'display': f'{code}_{doc}', 'code': code, 'doc': doc,
                'sort_key': list(_sort_key(code)), 'source': 'pou_evidence',
                'needs_review': False,
            }
    return identity
