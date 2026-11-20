"""Physical XML nets. References are resolved inside their export-defined namespace."""
import re
from collections import defaultdict

from lxml import etree

from plc_trace.analysis.boundary_logic import IDENT
from plc_trace.graph.bindings import named_arguments
from plc_trace.analysis.successor_graph import STParser, WireError, tag, text
from plc_trace.capture.plcopen_tasks import is_task_declaration, task_declaration
from plc_trace.source.omron_native import kind, scope_of
from plc_trace.source.plcopen_trace_control import _conditions, _networks, _scope
from plc_trace.source.smc2_controlflow import Grid, parameters
from plc_trace.source.smc2_source import iter_networks
from plc_trace.source.source_expressions import scalar_reads, source_condition, valid_scalar


def child_text(element, name):
    return text(element.find('./{*}' + name))


def port(node, attribute):
    parent = node.getparent()
    return parent.get(attribute, '') if parent is not None else ''


def simple_name(value):
    return bool(value and re.fullmatch(IDENT, value) and value.upper() not in {'TRUE', 'FALSE'})


def st_body(node, code):
    """Keep original text, including remaining source beyond the supported ST grammar."""
    code = code.strip()
    node['st'] = code
    parser = STParser(code, condition_parser=source_condition, strict=False, retain_source=True)
    try:
        parser.seq()
    except (ValueError, WireError, RecursionError, IndexError) as exc:
        node.setdefault('limits', []).append(str(exc)[:200])
    node['assignments'] = [
        {'line': code.count('\n', 0, offset) + 1, 'target': lhs, 'value': rhs, 'guard': guard.text(),
         'reads': sorted(_conditions(guard, '') | (set(scalar_reads(rhs)) if valid_scalar(rhs) else set()))}
        for lhs, rhs, guard, offset, _ in parser.statements
    ]
    if parser.skipped:
        node['other_statements'] = [
            {'line': code.count('\n', 0, parser.spans[s['token']][0]) + 1, **s}
            for s in parser.skipped
        ]


class Topology:
    def __init__(self):
        self.nets = []
        self.tasks = []
        self.units = []
        self.issues = []
        self.next_node = 0
        self.symbols = defaultdict(lambda: {'reads': [], 'writes': []})
        self.locals = defaultdict(set)
        self.globals = defaultdict(list)
        self.types = defaultdict(dict)
        self.calls = []
        self.formals = defaultdict(dict)
        self.output_pins = {}

    def net(self, scope, language, line, label='', comment=''):
        net = {'id': len(self.nets), 'scope': scope, 'language': language, 'line': line,
               'label': str(label), 'nodes': [], 'wires': []}
        if comment:
            net['comment'] = comment
        self.nets.append(net)
        return net

    def node(self, net, typ, label, line, key='', **properties):
        node = {'id': self.next_node, 'type': typ, 'label': label, 'line': line}
        self.next_node += 1
        if key != '':
            node['key'] = str(key)
        node.update({k: v for k, v in properties.items() if v})
        net['nodes'].append(node)
        return node

    def issue(self, net, line, reason, ref=''):
        self.issues.append({'net': net['id'], 'line': line, 'reason': reason, 'reference': str(ref)})

    def resolve(self, lookup, pending):
        for net, source, source_pin, target, target_pin, medium, line, *position in pending:
            choices = lookup.get(source, ())
            if len(choices) == 1:
                node_id, pin_name = choices[0]
                declared = self.output_pins.get(node_id)
                if declared is not None and source_pin and source_pin.casefold() not in declared:
                    self.issue(net, line, 'unknown_output_pin', str(source) + '.' + source_pin)
                    continue
                net['wires'].append([node_id, source_pin or pin_name, target, target_pin, medium, *position])
            else:
                reason = 'duplicate_source_id' if choices else 'missing_source_id'
                self.issue(net, line, reason, source)

    def plc_body(self, unit, body):
        scope, lang = _scope(unit), tag(body)
        if lang not in ('LD', 'FBD'):
            net = self.net(scope, lang, body.sourceline)
            node = self.node(net, lang, lang, body.sourceline)
            if lang == 'ST':
                st_body(node, text(body))
            else:
                self.issue(net, body.sourceline, 'unsupported_body_language', lang)
            return
        # localId has body scope, including cross-network rails. Detect collisions
        # instead of borrowing a similarly numbered pin in a different body.
        lookup, continuations, named, pending = defaultdict(list), [], defaultdict(list), []
        net = None
        for elem, network, comment, _ in _networks(body):
            typ, key = tag(elem), elem.get('localId')
            if typ == 'vendorElement' and any(tag(c) == 'ElementType' and text(c).lower() == 'networktitle'
                                               for c in elem.iter()):
                net = self.net(scope, lang, elem.sourceline, network, comment)
                continue
            if key is None or typ in ('comment', 'vendorElement'):
                if typ == 'vendorElement' and not any('parallelbranch' == text(c).lower()
                        for c in elem if tag(c) == 'alternativeText'):
                    if net is None:
                        net = self.net(scope, lang, body.sourceline)
                    self.issue(net, elem.sourceline, 'vendor_element_uninterpreted', key or '')
                continue
            if net is None:
                net = self.net(scope, lang, body.sourceline)
            name = elem.get('name', '')
            expr = child_text(elem, 'variable') or child_text(elem, 'expression')
            label = elem.get('typeName') or expr or name or typ
            props = {k: elem.get(k) for k in ('negated', 'edge', 'storage', 'instanceName', 'executionOrderId')}
            node = self.node(net, typ, label, elem.sourceline, key, **props)
            idx = node['id']
            outputs = elem.findall('./{*}outputVariables/{*}variable') + elem.findall('./{*}inOutVariables/{*}variable')
            if outputs:
                self.output_pins[idx] = {v.get('formalParameter','').casefold() for v in outputs}
            lookup[key].append((idx, ''))
            if typ == 'connector':
                named[name.casefold()].append((idx, ''))
            elif typ == 'continuation':
                continuations.append((net, name.casefold(), '', idx, '', 'connector', elem.sourceline))
            if expr and typ in ('contact', 'inVariable', 'inOutVariable', 'outVariable', 'coil'):
                node['operands'] = [['write' if typ in ('coil','outVariable') else 'read', expr]]
                if typ == 'inOutVariable':
                    node['operands'].append(['write', expr])
            for pin in elem.xpath('./*[local-name()="connectionPointIn"] | ./*/*[local-name()="variable"]/*[local-name()="connectionPointIn"]'):
                dest = port(pin, 'formalParameter')
                medium = ('control' if dest.upper() in ('EN', '_EN') or
                          (lang == 'LD' and not dest and typ in ('coil', 'contact', 'rightPowerRail'))
                          else 'pin')
                for conn in pin.findall('./{*}connection'):
                    param = pin.getparent()
                    ordinal = [param.getparent().index(param)+1] if not dest and tag(param)=='variable' else []
                    pending.append((net, conn.get('refLocalId'), conn.get('formalParameter', ''),
                                    idx, dest, medium, conn.sourceline, *ordinal))
            for section in elem:
                roles = {'outputVariables': 'write', 'inputVariables': 'read', 'inOutVariables': 'inout'}
                role = roles.get(tag(section))
                if role:
                    for param in section:
                        mods = {k:param.get(k) for k in ('negated','edge','storage')
                                if param.get(k,'').lower() not in ('','false','none')}
                        if mods:
                            node.setdefault('pin_modifiers', []).append([param.get('formalParameter',''), mods])
                        actual = (child_text(param, 'expression') or
                                  text(param.find('./{*}connectionPointOut/{*}expression')) or
                                  text(param.find('./{*}connectionPointIn/{*}expression')))
                        if actual:
                            node.setdefault('operands', []).append([role, actual, param.get('formalParameter', '')])
            snippets = elem.findall('.//{*}STCode')
            if snippets:
                if len(snippets) == 1:
                    st_body(node, text(snippets[0]))
                else:
                    self.issue(net, elem.sourceline, 'multiple_inline_st_sections', key)
        self.resolve(lookup, pending)
        self.resolve(named, continuations)

    def native_rung(self, rung):
        scope, _ = scope_of(rung)
        net = self.net(scope, 'LD', rung.sourceline, rung.get('evaluationOrder', ''),
                       '\n'.join(text(o) for o in rung if kind(o) == 'Comment'))
        outputs, pending = defaultdict(list), []
        for i, elem in enumerate(rung):
            typ = kind(elem)
            if typ == 'Comment':
                continue
            val = elem.get('operand') or elem.get('identifier') or ''
            node = self.node(net, typ, elem.get('typeName') or val or typ, elem.sourceline, i,
                             **{k: v for k, v in elem.attrib.items() if '}' not in k and
                                k not in ('operand', 'identifier', 'typeName')})
            if val and typ in ('Contact', 'Coil', 'DataSource', 'DataSink'):
                node['operands'] = [['write' if typ in ('Coil', 'DataSink') else 'read', val]]
            if typ == 'InlineST':
                leaves = [c for c in elem.iter('{*}ST') if not c.findall('./{*}ST')]
                if leaves:
                    st_body(node, '\n'.join(text(c) for c in leaves))
            for param in elem.iter('{*}InputVariable','{*}OutputVariable','{*}InOutVariable'):
                mods = {k:param.get(k) for k in ('negated','edge','storage')
                        if param.get(k,'').lower() not in ('','false','none')}
                if mods:
                    node.setdefault('pin_modifiers', []).append([param.get('parameterName',''),mods])
            for cp in elem.iter('{*}ConnectionPointOut'):
                key = cp.get('connectionPointOutId')
                if key is not None:
                    outputs[key].append((node['id'], port(cp, 'parameterName')))
            for cp in elem.iter('{*}ConnectionPointIn'):
                dest = port(cp, 'parameterName')
                medium = 'control' if dest.upper() in ('EN','_EN') or (not dest and typ in
                    ('Contact','Coil','RightPowerRail','Branch','InlineST')) else 'pin'
                for conn in cp.findall('./{*}Connection'):
                    pending.append((net, conn.get('refConnectionPointOutId'), '', node['id'],
                                    dest, medium, conn.sourceline))
        self.resolve(outputs, pending)

    def grid(self, source):
        audit = {}
        entries = set()
        for loc in iter_networks(source, audit):
            src, scope, line = loc['net'], loc['entry'], loc['source_line']
            if scope not in entries:
                self.units.append({'scope':scope, 'kind':'SMC2 entry', 'line':line})
                entries.add(scope)
            net = self.net(scope, 'SMC2', line, loc['network'], str(src.get('CMT') or ''))
            grid = Grid(src)
            nodes = []
            for i, cell in enumerate(src['CLs']):
                typ = cell.get('__type', '?')
                node = self.node(net, typ, str(cell.get('Name') or cell.get('Var') or typ), line, i,
                                 coords=[cell.get('X',0), cell.get('Y',0)],
                                 **{k:cell[k] for k in ('Not','Up','Dwn','Set','Rst') if cell.get(k)})
                nodes.append(node['id'])
                if typ == 'IST' and cell.get('TXT'):
                    st_body(node, str(cell['TXT']))
                if typ in ('LD', 'ST') and cell.get('Var'):
                    node['operands'] = [['write' if typ == 'ST' else 'read', cell['Var']]]
                for side, role in [('In','read'),('Out','write')]:
                    for pin_name, value in parameters(cell, side).items():
                        node.setdefault('operands', []).append([role, value, pin_name])
            if grid.error:
                self.issue(net, line, grid.error)
                continue
            for i, (x, y) in grid.coords.items():
                for before in grid.incoming[grid.find((x, y))]:
                    net['wires'].append([nodes[before], '', nodes[i], '', 'control'])
        for issue in audit['parse_issues']:
            self.issues.append({'line': issue.get('line'), 'reason': 'invalid_grid', 'detail': issue})

    def read(self, source, dialect):
        if dialect == 'smc2':
            self.grid(source)
            return
        doc = etree.parse(str(source), etree.XMLParser(huge_tree=True, no_network=True, resolve_entities=False))
        if dialect == 'plcopen':
            for unit in doc.iter('{*}pou', '{*}action', '{*}method'):
                scope = _scope(unit)
                self.units.append({'scope': scope, 'kind': unit.get('pouType') or tag(unit), 'line': unit.sourceline})
                for body in unit.findall('./{*}body/*'):
                    if isinstance(body.tag, str):
                        self.plc_body(unit, body)
            for elem in doc.iter('{*}task'):
                if is_task_declaration(elem):
                    self.tasks.append(task_declaration(elem))
        else:
            for unit in doc.iter('{*}Program','{*}FunctionBlock','{*}Function'):
                self.units.append({'scope':unit.get('name',''), 'kind':tag(unit), 'line':unit.sourceline})
            for body in doc.iter('{*}BodyContent'):
                scope, _ = scope_of(body)
                scope += '::' + body.get('name','ST')
                st = body.findall('./{*}ST')
                if st:
                    net = self.net(scope,'ST',body.sourceline)
                    st_body(self.node(net,'ST','ST',body.sourceline), '\n'.join(text(c) for c in st))
                elif body.find('.//{*}Rung') is None:
                    net = self.net(scope, kind(body), body.sourceline)
                    self.issue(net, body.sourceline, 'unsupported_body_language', kind(body))
            for rung in doc.iter('{*}Rung'):
                self.native_rung(rung)
        for group in doc.iter('{*}globalVars', '{*}GlobalVars'):
            # CODESYS can repeat a GVL under its own MixedAttrsVarList metadata.
            # This inner copy does not declare a second independently addressable GVL.
            if any(tag(x).lower() == 'globalvars' for x in group.iterancestors()):
                continue
            for var in group:
                if tag(var).lower() == 'variable' and var.get('name'):
                    self.globals[var.get('name').casefold()].append(var.sourceline)
        for unit in doc.iter('{*}pou','{*}Program','{*}FunctionBlock','{*}Function'):
            scope = _scope(unit) if tag(unit) == 'pou' else unit.get('name', '')
            self.locals.setdefault(scope.casefold(), set())
            for group in [*unit.findall('./{*}interface/*'), *unit.findall('./{*}Parameters/*'), *unit]:
                if tag(group).lower() in ('vars','localvars','tempvars','inputvars','outputvars','inoutvars'):
                    self.locals.setdefault(scope.casefold(), set())
                    for var in group:
                        role = {'inputvars':'read', 'outputvars':'write', 'inoutvars':'inout'}.get(tag(group).lower())
                        if role and tag(var).lower() == 'variable' and var.get('name'):
                            self.formals[scope.casefold()][var.get('name').casefold()] = role
                        type_name = var.find('./{*}Type/{*}TypeName')
                        if type_name is not None and var.get('name') and text(type_name):
                            self.types[scope.casefold()][var.get('name').casefold()] = text(type_name)
                        derived = var.find('./{*}type/{*}derived')
                        if derived is not None and var.get('name') and derived.get('name'):
                            self.types[scope.casefold()][var.get('name').casefold()] = derived.get('name')
                    self.locals[scope.casefold()].update(v.get('name').casefold() for v in group
                                                         if v.get('name') and tag(v).lower() == 'variable')

    def owner_scope(self, scope):
        parts = scope.casefold().split('::')
        return next(('::'.join(parts[:i]) for i in range(len(parts),0,-1)
                     if '::'.join(parts[:i]) in self.locals), None)

    def namespace(self, scope, name):
        root = re.split(r'[.\[]', name, 1)[0].casefold()
        owner = self.owner_scope(scope)
        if owner and root in self.locals[owner]:
            return 'local:' + owner
        if len(self.globals.get(root, ())) == 1:
            return 'global'
        return 'unresolved:' + scope.casefold()

    def sample_address(self, scope, operand):
        from plc_trace.capture.step_discovery import fixed_path
        if not fixed_path(operand):
            return None
        root = re.split(r'[.\[]', operand, 1)[0].casefold()
        owner = self.owner_scope(scope)
        if owner and root in self.locals[owner]:
            if owner not in self.program_scopes:
                return None
            return owner.rsplit('::',1)[-1] + '.' + operand.casefold()
        if len(self.globals.get(root, ())) == 1:
            return operand.casefold()
        return None

    def index_operands(self):
        # Index exact operands only. Global scope needs a unique declaration;
        # undeclared/dynamic references are navigable occurrences, never alias proof.
        self.program_scopes = {u['scope'].casefold() for u in self.units if u['kind'].lower() == 'program'}
        definitions = defaultdict(list)
        for unit in self.units:
            definitions[unit['scope'].rsplit('::',1)[-1].casefold()].append(unit)
        for net in self.nets:
            scope = net['scope'].casefold()
            owner = self.owner_scope(scope)
            for node in net['nodes']:
                refs = [(r[0], r[1], r[2] if len(r)>2 else '') for r in node.get('operands', ())]
                for a in node.get('assignments', ()):
                    pin = 'ST ' + str(a['line'])
                    refs.append(('write', a['target'], pin))
                    refs.extend(('read', v, pin) for v in a['reads'])
                call_names = []
                if node['type'].casefold() in ('block','f','fb'):
                    call_names.append((node['label'], None, node.get('instanceName', '')))
                for statement in node.get('other_statements', ()):
                    fn = re.match(r'([\w.]+)\s*\(', statement['stmt'])
                    if fn:
                        call_names.append((self.types[owner].get(fn[1].casefold(), fn[1]), statement, fn[1]))
                for callee, stmt, instance in call_names:
                    st_line = stmt['line'] if stmt is not None else 0
                    options = definitions.get(callee.casefold(), ())
                    actions = [u for u in options if u['kind'] in ('action','method')
                               and owner and u['scope'].rpartition('::')[0].casefold() == owner]
                    targets = actions or [u for u in options if u['kind'] not in ('action','method')]
                    if targets:
                        call = {'net':net['id'], 'node':node['id'], 'type':callee,
                                           'st_line':st_line, 'targets':[u['scope'] for u in targets],
                                           'resolved':len(targets) == 1, 'instance':instance}
                        if stmt is not None and call['resolved']:
                            args = named_arguments(stmt['stmt'], self.formals[targets[0]['scope'].casefold()])
                            if args:
                                call['arguments'] = args
                                call['st_token'] = stmt['token']
                                refs.extend((role, actual, f"ST {st_line} · {pin} @{stmt['token']}")
                                            for role, actual, pin in args)
                        self.calls.append(call)
                for role, name, pin in refs:
                    if not simple_name(name):
                        continue
                    namespace = self.namespace(scope,name)
                    bucket = self.symbols[(namespace, name.casefold())]
                    bucket['name'] = name
                    for direction in ('reads','writes') if role == 'inout' else (('writes',) if role=='write' else ('reads',)):
                        bucket[direction].append([net['id'], node['id'], pin])
