"""Record source-backed actual/formal pin bindings at each distinct source call."""
import re
from collections import defaultdict

from plc_trace.capture.step_discovery import fixed_path


def named_arguments(statement, declared):
    """Accept named, fixed-address actuals. Never bind a positional or computed value."""
    body = re.fullmatch(r'[\w.]+\s*\((.*)\)\s*;?', statement, re.S)
    if not body or not declared or any(q in body[1] for q in ("'", '"')):
        return []
    parts = []
    depth = start = 0
    for pos, char in enumerate(body[1]):
        if char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
            if depth < 0:
                return []
        elif char == ',' and not depth:
            parts.append(body[1][start:pos])
            start = pos + 1
    if depth:
        return []
    parts.append(body[1][start:])
    result = []
    seen = set()
    for part in parts:
        match = re.fullmatch(r'\s*([\w]+)\s*(:=|=\s*>)\s*(.*?)\s*', part, re.S)
        if not match:
            continue
        pin = match[1].casefold()
        if pin in seen:
            return []
        seen.add(pin)
        role = declared.get(pin)
        actual = re.sub(r'\s*([.\[\],])\s*', r'\1', match[3])
        if (role == 'write' and match[2].startswith('=') or
                role in ('read', 'inout') and match[2] == ':=') and fixed_path(actual):
            result.append([role, actual, match[1]])
    return result


def call_bindings(nets, calls, symbol_ids, namespace, formals):
    nodes = {node['id']: node for net in nets for node in net['nodes']}
    wires = defaultdict(list)
    for net in nets:
        for wire in net['wires']:
            wires[wire[0]].append(wire)
            if wire[2] != wire[0]:
                wires[wire[2]].append(wire)
    bindings = []
    for i, call in enumerate(calls):
        if not call['resolved']:
            continue
        node = nodes[call['node']]
        scope = nets[call['net']]['scope']
        found = set()
        declared = formals.get(call['targets'][0].casefold(), {})

        def bind(role, actual, pin, source_id):
            if not pin or not fixed_path(actual):
                return
            owner = namespace(scope, actual)
            sid = symbol_ids.get((owner, actual.casefold()))
            direction = declared.get(pin.casefold(), role)
            if direction != role and direction != 'inout' and role != 'inout':
                return
            if sid is not None and not owner.startswith('unresolved:'):
                found.add((pin, direction, sid, source_id))

        if call['st_line']:
            for role, actual, pin in call.get('arguments', ()):
                bind(role, actual, pin, node['id'])
            if found:
                bindings.append([i, sorted(found)])
            continue
        for operand in node.get('operands', ()):
            if len(operand) == 3:
                bind(*operand, node['id'])
        for src, output, dest, input_, *_ in wires[node['id']]:
            if dest == node['id'] and nodes[src]['type'].lower() in ('invariable', 'datasource'):
                bind('read', nodes[src]['label'], input_, src)
            if src == node['id'] and nodes[dest]['type'].lower() in ('outvariable', 'datasink'):
                bind('write', nodes[dest]['label'], output, dest)
        if found:
            bindings.append([i, sorted(found)])
    return bindings


def instance_dependencies(top, symbol_ids, symbol_keys, bindings):
    """Specialize a direct formal-to-formal assignment for one proven call site."""
    bodies = defaultdict(list)
    for net in top.nets:
        scope = net['scope'].casefold()
        owner = top.owner_scope(scope)
        if not owner or scope not in (owner, owner + '::st') or not top.formals.get(owner):
            continue
        roles = top.formals[owner]
        for node in net['nodes']:
            for assignment in node.get('assignments', ()):
                lhs = assignment['target'].casefold()
                if roles.get(re.split(r'[.\[]', lhs, 1)[0]) not in ('write','inout'):
                    continue
                sources = [name.casefold() for name in assignment['reads']
                           if roles.get(re.split(r'[.\[]', name.casefold(), 1)[0]) in ('read','inout')]
                if sources:
                    bodies[owner].append((lhs, sources, net['id'], node['id'], assignment['line']))

    result = []
    for i, ports in bindings:
        call = top.calls[i]
        target = call['targets'][0].casefold()
        if target not in bodies:
            continue
        caller = top.owner_scope(top.nets[call['net']]['scope'])
        if caller not in top.program_scopes:
            continue
        actuals = defaultdict(set)
        for pin, _, sid, _ in ports:
            actuals[pin.casefold()].add(symbol_keys[sid])

        def resolve(formal):
            if not fixed_path(formal):
                return None
            root = re.split(r'[.\[]', formal, 1)[0]
            matched = actuals.get(root)
            if not matched or len(matched) != 1:
                return None
            ns, path = next(iter(matched))
            if ns != 'global' and ns != 'local:' + caller:
                return None
            return symbol_ids.get((ns, path + formal[len(root):]))

        for lhs, operands, net, node, line in bodies[target]:
            dest = resolve(lhs)
            if dest is None:
                continue
            sources = {sid for name in operands if (sid := resolve(name)) is not None and sid != dest}
            if sources:
                result.append([dest, sorted(sources), net, node, 'instance_st', line, i])
    return result
