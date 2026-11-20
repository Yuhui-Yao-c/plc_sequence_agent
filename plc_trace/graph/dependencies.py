"""Direct signal dependencies with a concrete assignment or wired output as witness."""
from collections import defaultdict

from plc_trace.capture.step_discovery import fixed_path


def signal_dependencies(nets, symbol_ids, namespace, program_scopes):
    nodes = {}
    incoming = defaultdict(list)
    for net in nets:
        for n in net['nodes']:
            nodes[n['id']] = n
        for wire in net['wires']:
            incoming[wire[2]].append(wire)
    deps = []

    def address(scope, expr):
        if not expr or not fixed_path(expr):
            return None
        ns = namespace(scope, expr)
        # Reusable template locals have no single runtime identity. A template's
        # internal statements remain visible at its own source/call locations.
        if ns != 'global' and not (ns.startswith('local:') and ns[6:] in program_scopes):
            return None
        return symbol_ids.get((ns, expr.casefold()))

    for net in nets:
        scope = net['scope']
        for n in net['nodes']:
            def add(target, operands, witness, line=0):
                dest = address(scope, target)
                if dest is None:
                    return
                sources = {sid for operand in operands if (sid := address(scope, operand)) is not None
                           and sid != dest}
                if sources:
                    deps.append([dest, sorted(sources), net['id'], n['id'], witness, line])

            for a in n.get('assignments', ()):
                add(a['target'], a['reads'], 'st', a['line'])
            typ = n['type'].casefold()
            if typ not in ('coil', 'datasink', 'outvariable', 'block', 'st'):
                continue
            move = typ == 'block' and n['label'].upper() == 'MOVE'
            if typ == 'block' and not move:
                continue
            if typ == 'st' and net['language'] != 'SMC2':
                continue
            targets = [v[1] for v in n.get('operands', ()) if v[0] == 'write' and
                       (not move or len(v) < 3 or v[2].upper() not in ('ENO', '_ENO'))]
            if not targets:
                continue
            # Follow only known connective semantics. The pins of an arbitrary
            # FB may have unrelated meanings, even on a physically connected rung.
            stack = [n['id']]
            visited, inputs = set(), set()
            while stack:
                at = stack.pop()
                if at in visited:
                    continue
                visited.add(at)
                for src, outpin, _, inpin, medium, *_ in incoming[at]:
                    peer = nodes[src]
                    t = peer['type'].lower()
                    if (t in ('contact', 'ld') and medium == 'control'):
                        inputs.update(o[1] for o in peer.get('operands', ()) if o[0] == 'read')
                        stack.append(src)
                    elif t in ('connector', 'continuation', 'hl', 'branch'):
                        stack.append(src)
                    elif t in ('invariable', 'datasource'):
                        inputs.add(peer['label'])
                    elif t == 'block' and peer['label'].upper() == 'MOVE' and outpin.upper() not in ('EN','ENO','_ENO'):
                        stack.append(src)
            if move:
                inputs.update(o[1] for o in n.get('operands', ()) if o[0] == 'read')
            for target in targets:
                add(target, inputs, 'wire')
    return deps
