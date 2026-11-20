"""Port-sensitive signal slices for individual writes, including their execution enables."""
from collections import defaultdict

from plc_trace.capture.step_discovery import fixed_path
from plc_trace.source.plcopen_trace_control import _COMPARATORS, _ARITHMETIC_BLOCKS

# IEC operators with one result. Project-defined calls take precedence over these mnemonics.
_OPERATORS = frozenset(_COMPARATORS) | frozenset(_ARITHMETIC_BLOCKS) | {'MOVE', 'AND', 'OR', 'XOR', 'NOT'}
_ENABLE = {'EN', '_EN'}
_ENABLE_OUT = {'ENO', '_ENO'}
_SOURCES = {'invariable', 'datasource'}
_CONTACTS = {'contact', 'ld'}
_CONNECTORS = {'connector', 'continuation', 'hl', 'branch'}
_PASS_FLOW = _CONTACTS | _CONNECTORS | {'coil','st'}


class WireSlice:
    def __init__(self, nets, calls):
        self.nodes = {n['id']: n for net in nets for n in net['nodes']}
        self.incoming = defaultdict(list)
        for net in nets:
            for wire in net['wires']:
                self.incoming[wire[2]].append(wire)
        self.user_blocks = {c['node'] for c in calls if not c['st_line']}
        self.operators = {i for i, n in self.nodes.items() if n['type'].lower() in ('block', 'f')
                          and not n.get('instanceName') and i not in self.user_blocks
                          and n['label'].upper() in _OPERATORS}

    def trace(self, node, coil=False, enable_only=False):
        reads = {'when': set(), 'value': set()}
        stops, visited, stack = set(), set(), []

        def operands(n, downstream, only_enable=False, *, target=False, status=False):
            # EN on the destination gates this write. EN on a data producer gates the
            # producer's calculation, which need not prevent the destination writing.
            def role(enable):
                if not enable or status:
                    return downstream
                return 'when' if target or downstream == 'when' else 'value_enable'

            for access, expr, *pins in n.get('operands', ()):
                enable = bool(pins) and pins[0].upper() in _ENABLE
                if access == 'read' and (not only_enable or enable):
                    reads.setdefault(role(enable), set()).add(expr)
            for src, outpin, _, pin, medium, *_ in self.incoming[n['id']]:
                enable = pin.upper() in _ENABLE or (medium == 'control' and n['type'].lower() not in _PASS_FLOW)
                if not only_enable or enable:
                    stack.append((src, outpin, role(enable)))

        operands(node, 'when' if coil else 'value', enable_only, target=True)
        while stack:
            ident, pin, role = stack.pop()
            key = (ident, pin.casefold(), role)
            if key in visited:
                continue
            visited.add(key)
            n = self.nodes[ident]
            typ = n['type'].lower()
            if typ in ('leftpowerrail', 'rightpowerrail'):
                continue
            if typ in _SOURCES:
                reads.setdefault(role, set()).add(n['label'])
            elif typ in _CONTACTS or typ in _CONNECTORS:
                operands(n, role)
            elif typ in ('block', 'f', 'fb'):
                port = (pin or n.get('main_output', '')).upper()
                if not port and n.get('ambiguous_output'):
                    stops.add((ident, '', 'ambiguous_port'))
                elif port in _ENABLE_OUT:
                    # ENO signals execution status. The data pins do not carry its value.
                    operands(n, role, only_enable=True, status=True)
                    if ident not in self.operators or n['label'].upper() in _ARITHMETIC_BLOCKS:
                        stops.add((ident, port, 'block_status'))
                elif ident in self.operators and (not port or port in ('OUT', '_OUT') or
                                                   port == n.get('main_output', '').upper()):
                    operands(n, role)
                else:
                    stops.add((ident, pin, 'block_output'))
            else:
                stops.add((ident, pin, 'element_output'))
        return reads, stops


def signal_dependencies(nets, symbol_ids, namespace, program_scopes, *, sites=(), calls=(),
                        roles=None, boundaries=None):
    rows = []
    wire = WireSlice(nets, calls)
    guards = defaultdict(list)
    for i, site in enumerate(sites):
        if site['source_node'] is not None and not site.get('binding'):
            key = site['net'], site['source_node'], site['channel'].casefold()
            guards[key].append(i)

    def address(scope, expr):
        if not expr or not fixed_path(expr):
            return None
        ns = namespace(scope, expr)
        if ns != 'global' and not (ns.startswith('local:') and ns[6:] in program_scopes):
            return None
        return symbol_ids.get((ns, expr.casefold()))

    for net in nets:
        for n in net['nodes']:
            def add(target, conditions, values, witness, *, line=0, statement=None, stops=(), outer=(), value_enable=()):
                scope = net['scope']
                dest = address(scope, target)
                if dest is None:
                    return
                when = {address(scope, v) for v in conditions}
                data = {address(scope, v) for v in values}
                enabled = {address(scope, v) for v in value_enable}
                site_ids = []
                if witness == 'wire':
                    # Link the matching transition without using its separate expression parser to invent a wire.
                    site_ids = guards.get((net['id'], n['id'], target.casefold()), ())
                for ids in (when, data, enabled):
                    ids.discard(None)
                    ids.discard(dest)
                if stops and boundaries is not None:
                    boundaries.append([dest, net['id'], n['id'], line, sorted(stops)])
                if when or data or enabled:
                    rows.append([dest, sorted(when | data | enabled), net['id'], n['id'], witness, line])
                    if roles is not None:
                        detail = {'when': sorted(when), 'value': sorted(data)}
                        if enabled:
                            detail['value_enable'] = sorted(enabled)
                        physical = {address(scope, v) for v in outer} & when
                        if physical:
                            detail['enable'] = sorted(physical)
                        if statement is not None:
                            detail['statement'] = statement
                        if site_ids:
                            detail['sites'] = site_ids
                        roles.append(detail)

            assignments = n.get('assignments', ())
            if assignments:
                outer, limits = wire.trace(n, enable_only=True)
                for j, a in enumerate(assignments):
                    add(a['target'], outer['when'].union(a['when_reads']), a['value_reads'], 'st',
                        line=a['line'], statement=j, stops=limits, outer=outer['when'])

            typ = n['type'].lower()
            move = n['id'] in wire.operators and n['label'].upper() == 'MOVE'
            coil = typ == 'coil' or (typ == 'st' and net['language'] == 'SMC2')
            if not (coil or move or typ in ('datasink', 'outvariable')):
                continue
            targets = [o[1] for o in n.get('operands', ()) if o[0] == 'write' and
                       (len(o) < 3 or o[2].upper() not in _ENABLE_OUT)]
            if targets:
                # A normal coil stores the rung value. S/R coils change memory when energized.
                latched = (str(n.get('storage') or n.get('latch') or '').casefold() in ('set','reset') or
                           any(str(n.get(k, '')).casefold() in ('true','1') for k in ('Set','Rst')))
                selected, stops = wire.trace(n, coil=coil and latched)
                for target in targets:
                    add(target, selected['when'], selected['value'], 'wire', stops=stops,
                        value_enable=selected.get('value_enable', ()))
    return rows
