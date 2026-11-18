"""Exact declaration, instance and control evidence for station-level capture review."""
import re

from plc_trace.analysis.boundary_logic import symbol


def family(name):
    """Reading groups only. Group membership never selects another address."""
    return re.sub(r'\d+', '#', name.casefold())


def declaration(node):
    """Only the declared object's own prose and expressly indexed element notes."""
    doc = next((child for child in node if isinstance(child.tag, str)
                and child.tag.rsplit('}', 1)[-1].lower() == 'documentation'), None)
    raw = ''.join(doc.itertext()).strip() if doc is not None else ''
    # Some PLCopen exports append a watch table to the author's instance label.
    group = re.search(r'<GrpComment_zh-cn>(.*?)</GrpComment_zh-cn>', raw, re.S)
    raw = re.split(r'<Sub\s*/?>', group[1] if group else raw, maxsplit=1)[0]
    note = ' '.join((re.sub(r'<[^>]+>', '', raw) if group else raw).split())
    elements = {}
    for item in node.iter('{*}ElementComment'):
        owner = next((p for p in item.iterancestors() if p is node or
                      p.tag.rsplit('}', 1)[-1].lower() in {'variable', 'member'}), None)
        if owner is not node:
            continue
        address = symbol(item.get('element', '')).casefold()
        texts = [' '.join(t.itertext()).strip() for t in item.findall('{*}Text')]
        if address and any(texts):
            elements.setdefault(address, []).append(
                {'line': item.sourceline, 'text': ' / '.join(t for t in texts if t)})
    return {'name': node.get('name'), 'line': node.sourceline, 'text': note}, elements


def at_path(definition, parts, index):
    if definition is None:
        return []
    site, elements = definition
    prefix = '.'.join(parts[:index+1])
    field = parts[index].split('[', 1)[0]
    suffix = '.'.join(parts[index:])[len(field):].casefold()
    result = [{**site, 'kind': 'declaration', 'path': prefix}]
    if suffix:
        result.extend({**item, 'kind': 'element', 'path': '.'.join(parts)} for item in elements.get(suffix, []))
    return result


def required_cursors(expr):
    """Exact positive integer comparisons required on every executable arm."""
    if expr.kind == 'eq':
        return {expr.args[0].casefold()}
    if expr.kind in {'and', 'or'}:
        arms = [required_cursors(item) for item in expr.args]
        return set.union(*arms) if expr.kind == 'and' else set.intersection(*arms)
    return set()


def writer_context(site):
    calls = tuple((c['scope'], c['instance'], c['source_line']) for c in site.get('binding', ()))
    return site['scope'], calls


def same_station_array(row, decision, peer, reference):
    # A fixed slot of the same declared station array, within the same real host
    # program. A similar member of another instance or array cannot attest it.
    def storage(candidate):
        m = re.fullmatch(r'(.+\[)(-?\d+(?:,-?\d+)*)\]', candidate['identity'])
        if not m:
            return None
        # For a multidimensional array the preceding coordinates identify the unit.
        return m[1], tuple(m[2].split(',')[:-1])

    def source(candidate, choice):
        scope = choice['owner_scope']
        declarations = tuple(d['line'] for d in candidate['declarations'] if d['kind'] == 'declaration')
        programs = {t.get('program') for t in candidate['transitions']
                    if t['scope'] == scope and t['id'] in choice['basis']}
        return declarations, programs

    if not peer or storage(row) is None or storage(row) != storage(peer):
        return False
    decl, programs = source(row, decision)
    other, hosts = source(peer, reference)
    return bool(decl and all(decl) and decl == other and len(programs) == 1 and all(programs) and programs == hosts)


def dependent_writes(selected):
    """Withhold a cursor whose every recorded write is subordinate to another selected cursor.

    Require the peer to have an independent write in that very source routine.
    A mere read, inter-station handshake or one shared transition cannot veto a channel.
    """
    independent = set()
    for key, row in selected.items():
        for site in row['transitions']:
            if selected.keys().isdisjoint(site.get('co_cursors', ())):
                independent.add((key, writer_context(site)))
    withheld = {}
    for key, row in selected.items():
        peers = set()
        for site in row['transitions']:
            gates = {p for p in site.get('co_cursors', ()) if (p, writer_context(site)) in independent}
            if not gates:
                break
            peers.update(gates)
        else:
            withheld[key] = sorted(peers)
    return withheld
