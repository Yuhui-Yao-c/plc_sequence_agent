"""Independent commissioning references and verification of the serialized capture channels."""
import csv
import io
import re
from collections import defaultdict
from xml.etree import ElementTree as ET

from openpyxl import load_workbook

from plc_trace.analysis.boundary_logic import IDENT, comparisons, symbol
from plc_trace.source.source_expressions import source_condition
from plc_trace.trace.trace_to_pairs import INTEGER_TYPES, REAL_TYPES


MEMBER_COLUMNS = ('结构类型', '成员', '数据类型', '地址数', '示例地址')


def read_member_types(path):
    """Read independently verified scalar member declarations. Empty types remain unresolved."""
    if not path:
        return {}
    declarations = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        if not set(MEMBER_COLUMNS[:3]).issubset(reader.fieldnames or ()):
            raise ValueError('成员类型表需要“结构类型、成员、数据类型”三列')
        for row in reader:
            owner, member, kind = ((row[k] or '').strip() for k in MEMBER_COLUMNS[:3])
            if not all(re.fullmatch(r'[^\W\d]\w*', token) for token in (owner, member)):
                raise ValueError(f'成员类型表第 {reader.line_num} 行缺少具体的结构类型或成员名')
            if not kind:
                continue
            key, kind = (owner.casefold(), member.casefold()), kind.upper()
            if kind not in INTEGER_TYPES | REAL_TYPES or declarations.get(key, kind) != kind:
                raise ValueError(f'成员类型冲突或不适用：{owner}.{member} {kind}')
            declarations[key] = kind
    return declarations


def _table(path):
    if path.suffix.lower() == '.xlsx':
        book = load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet in book.worksheets:
                yield list(sheet.values)
        finally:
            book.close()
        return
    raw = path.read_bytes()
    enc = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    text = raw.decode(enc)
    lines = text.splitlines()
    if not any(line.strip() for line in lines):
        raise ValueError(f'核验输入为空：{path.name}')
    if all(re.fullmatch(IDENT, line.strip()) for line in lines if line.strip()):
        yield [[line.strip()] for line in lines if line.strip()]
    else:
        delimiter = '\t' if '\t' in lines[0] else ','
        yield list(csv.reader(io.StringIO(text, newline=''), delimiter=delimiter))


def _states(cell):
    result = defaultdict(set)
    for atom in comparisons(source_condition(str(cell or ''))):
        if atom.kind == 'eq':
            result[symbol(atom.args[0]).casefold()].add(atom.args[1])
    return result


def read_reference(path, candidates):
    """Direct lists are verified addresses. Timing tables use changing endpoints and source selectors."""
    names, types, unresolved = {}, {}, []
    state = {r['identity'] for r in candidates if r['sequence_witness'] or r['control_links']
             or r['eligibility'] in ('single_write', 'distributed_write')}
    loose = []
    def add(value, kind=''):
        value = str(value or '').strip()
        if not re.fullmatch(IDENT, value) or any(not re.fullmatch(r'-?\d+(?:,-?\d+)*', sub)
                                                for sub in re.findall(r'\[([^]]*)\]', value)):
            raise ValueError(f'核验地址须为具体变量：{value}')
        key = symbol(value).casefold()
        names[key] = symbol(value)
        kind = str(kind or '').strip().upper()
        if kind:
            if kind not in INTEGER_TYPES | REAL_TYPES or (key in types and types[key] != kind):
                raise ValueError(f'核验地址的类型冲突或不适用：{value} {kind}')
            types[key] = kind

    for rows in _table(path):
        for i, header in enumerate(rows):
            cols = [str(v or '').strip().casefold() for v in header]
            namecol = next((j for j,h in enumerate(cols) if h in ('步序变量','变量','地址','name','variable')), None)
            starts = [j for j,h in enumerate(cols) if h in ('开始','开始条件','开始标签','start')]
            ends = [j for j,h in enumerate(cols) if h in ('结束','结束条件','结束标签','end')]
            if starts and len(starts) == len(ends) and all(a < b for a,b in zip(starts, ends)):
                for number,row in enumerate(rows[i+1:], i+2):
                    for a,b in zip(starts, ends):
                        x = _states(row[a]) if a < len(row) else {}
                        y = _states(row[b]) if b < len(row) else {}
                        varied = {k for k in x.keys() & y if x[k].isdisjoint(y[k])}
                        for k in varied:
                            add(k)
                        if not varied and (x or y):
                            changed = {k for k in x.keys() | y.keys() if x.get(k) != y.get(k)}
                            loose.append((number, set(x) | set(y), changed))
                        elif not x and not y:
                            cells = [str(row[j] or '') for j in (a,b) if j < len(row)]
                            if any(re.search(r'[=<>]', cell) for cell in cells):
                                unresolved.append({'row': number, 'conditions': cells})
                break
            if namecol is not None:
                tc = next((j for j,h in enumerate(cols) if h in ('类型','数据类型','type','data type')), None)
                for row in rows[i+1:]:
                    if namecol < len(row) and row[namecol]:
                        add(row[namecol], row[tc] if tc is not None and tc < len(row) else '')
                break
            if cols[:1] == ['index'] and 'rawtime' in cols:
                for cell in header:
                    n,sep,t = str(cell or '').strip().rpartition(':')
                    if sep and t.upper() in INTEGER_TYPES | REAL_TYPES:
                        add(n, t)
                break
        else:
            if len(rows) and all(len(row)==1 for row in rows):
                for row in rows:
                    add(row[0])
    related = set().union(*(ops for _, ops, _ in loose)) if loose else set()
    gates = {r['identity']: {name for guard in {t.get('operating_guard') for t in r['transitions']}
                             if guard for name in _states(guard)}
             for r in candidates if r['identity'] in related}
    for number, operands, changed in loose:
        supported = operands & (state | names.keys())
        unknown = (changed - supported) if supported else operands
        for key in supported:
            unknown = unknown - gates.get(key, set())
        if unknown:
            unresolved.append({'row':number, 'operands':sorted(unknown)})
        for key in supported:
            add(key)
    if not names and not unresolved:
        raise ValueError(f'未读到步序地址或起止条件：{path.name}')
    return names, types, unresolved


def referenced_channels(scan, discovery, missing):
    """Recover a verified step that appears in this export without a recognized state write."""
    found = {}
    if not missing:
        return found
    if scan.source_format == 'omron_iec61131_10':
        for (_, unit), (spelling, _, line) in scan.references.items():
            address, kind, issue, _ = scan.sysmac_channel(spelling, unit)
            key = address.casefold()
            if key not in missing or key in found:
                continue
            if kind:
                found[key] = {'name': address, 'source_line': line, 'type': kind, 'type_source': '声明'}
            elif not issue.startswith('功能块') and (slot := scan.scalar_member(address)) and slot[2]:
                found[key] = {'name': address, 'source_line': line, 'type': None, 'type_source': '',
                              'type_issue': f'缺少 {slot[0]}.{slot[1]} 的叶声明'}
    else:
        for key in missing & discovery.reference_sites.keys():
            row = discovery.candidates[key]
            if row.declared and not row.reasons - {'reference_only', 'no_constrained_state_change'}:
                found[key] = {'name': row.name, 'source_line': discovery.reference_sites[key]['source_line']}
    return found


def verify_config(contents, fmt, channels):
    """Re-open the exact bytes for publication, including both address lists in a native trace."""
    expected = {c['name'].casefold(): c.get('type') if fmt == 'sysmac' else None for c in channels}
    if fmt == 'sysmac':
        headers = [r[5:] for r in csv.reader(io.StringIO(contents.decode('utf-8-sig')))
                   if r[:1] == ['Index']]
        if len(headers) != 1:
            raise ValueError('采集配置回读缺少唯一的通道表头')
        columns = [v.rpartition(':') for v in headers[0]]
        entries = [(n.casefold(), t) for n, _, t in columns]
    else:
        root = ET.fromstring(contents)
        vars_ = root.findall(".//Single[@Name='Record']/Single[@Name='VariableList']/List/*/Single[@Name='VariableName']")
        entries = [((n.text or '').casefold(), None) for n in vars_]
        samples = [s.get('VarName', '').casefold() for s in root.findall('./TraceData/TraceRecord/TraceVariable')]
        if samples != [n for n, _ in entries]:
            raise ValueError('采集配置回读的采样地址与配置地址不一致')
    if len(expected) != len(channels) or len(entries) != len(expected) or dict(entries) != expected:
        raise ValueError('采集配置回读与应写通道的地址或类型不一致')
    return set(expected)
