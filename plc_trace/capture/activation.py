"""Inspect how a concrete private cursor can leave its declared initial state."""
import re
from bisect import bisect_left
from collections import defaultdict
from html import unescape
from xml.parsers import expat

from lxml import etree

from plc_trace.analysis.boundary_logic import IDENT, TRUE, Expr, combine, compatible, constrained_states, integer, negate
from plc_trace.analysis.fb_transport import required_signals
from plc_trace.analysis.successor_graph import STParser
from plc_trace.capture.step_discovery import isolate_counted_loops
from plc_trace.source.plcopen_trace_control import ERRORS
from plc_trace.source.source_expressions import INTEGER_BOUNDS, source_condition


def _tag(node):
    return node.tag.rsplit('}', 1)[-1].lower() if isinstance(node.tag, str) else ''


def _owner(name):
    return name.rsplit('::', 1)[-1].casefold()


def _storage(row):
    """Only an unambiguously bound POU-local scalar can be closed over its own writes."""
    owners = set()
    for site in row['transitions']:
        calls = site.get('binding', ())
        host = _owner(site.get('program', ''))
        parts = [host, *(c['instance'].casefold() for c in calls)]
        leaf = row['identity'].rsplit('.', 1)[-1]
        if row['identity'] != '.'.join([*parts, leaf]) or not re.fullmatch(r'[^\W\d]\w*', leaf):
            return None
        owners.add((_owner(calls[-1]['type']) if calls else host, leaf))
    return owners.pop() if len(owners) == 1 else None


def _initial(var):
    group = var.getparent()
    if (_tag(group) not in ('vars', 'localvars') or
            group.get('accessSpecifier', '').lower() != 'private' or
            any(k != 'accessSpecifier' for k in group.attrib) or set(var.attrib) != {'name'}):
        return None
    children = {_tag(n): n for n in var}
    if children.keys() - {'type', 'documentation', 'initialvalue'}:
        return None
    if 'initialvalue' not in children:
        return 0
    init = children['initialvalue']
    values = [n.get('value') for n in init.iter() if n.get('value') is not None]
    return integer(values[0] if len(values) == 1 else ''.join(init.itertext()))


def _read_only(before, after):
    """A direct comparison, CASE selector or plain value copy cannot pass a writable reference."""
    if re.search(r'(?:=>|\bREF\s*=)\s*$', before, re.I):
        return False
    return bool(re.match(r'\s*(?:OF\b|[<>=])', after, re.I) or
                re.search(r'(?<!:)[=<>]\s*$', before) or
                (re.search(rf'(?:^|;|\bTHEN|\bELSE)\s*{IDENT}\s*:=\s*$', before, re.I) and
                 re.match(r'\s*;', after)))


def _required_inputs(condition, calls, block, offset, seen=frozenset()):
    """TON and rising-edge outputs imply their input at activation, within this POU."""
    result = {}
    for port, positive in sorted(required_signals(condition)):
        if not positive:
            continue
        if port in calls and port not in seen:
            upstream = calls[port]
            if len(upstream) == 1 and upstream[0]:
                expr, line, section, at = upstream[0]
                if block != section or at >= offset:
                    continue
                for name, chain in _required_inputs(expr, calls, block, at, seen | {port}).items():
                    result[name] = [line, *chain]
        else:
            result[port] = []
    return result


def _section_coordinates(block, code):
    """Use the same ST section and character offsets as the Sysmac write index."""
    from plc_trace.source.omron_native import content, scope_of

    body = block.getparent()
    if _tag(body) != 'bodycontent' or _tag(block) != 'st':
        return {}, 0
    scope, _ = scope_of(body)
    preceding = [n for n in block.itersiblings(preceding=True) if _tag(n) == 'st']
    # collect_native joins each direct ST child's stripped text with one newline.
    shift = sum(len(content(n)) + 1 for n in preceding) - (len(code) - len(code.lstrip()))
    return {'scope': scope + '::' + body.get('name', 'ST'), 'network': body.get('evaluationOrder', 'ST')}, shift


_TERMINAL_IF = re.compile(rf'\s*=\s*(-?\d+)\s+THEN\s+({IDENT})\s*:=\s*(-?\d+)\s*;\s*END_IF\b', re.I)


def _read_use(parsed, dispatches, previous, identity, clean, pos, end, locator, shift):
    """Recognize only a direct self dispatch or a terminal test with one consumer."""
    token, start = previous.get(pos, ('', -1))
    if locator and token == 'CASE' and (start, identity) in dispatches:
        return {'kind': 'dispatch', 'offset': start + shift}
    if locator and token == 'IF' and (test := _TERMINAL_IF.match(clean, end)):
        writer = test.start(2)
        statement = parsed.get(writer)
        if statement and statement[0] == test[2].casefold() and integer(statement[1]) == int(test[3]):
            return {'kind': 'parent_exit', 'value': int(test[1]), 'target': test[2],
                    'to': int(test[3]), 'write_offset': writer + shift}
    return {'kind': 'other'}


def _block_facts(code, block, origin, wanted, types, facts, calls, flags, cited_lines, controlled):
    from plc_trace.capture.dispatch_operations import dispatch_operations
    from plc_trace.capture.extract_step_variables import _executable_text
    from plc_trace.capture.sysmac_discovery import _args

    clean = ''.join(_executable_text(code, keep_positions=True))
    safe_code = ''.join(_executable_text(code, keep_positions=True, keep_strings=True))
    locator, shift = _section_coordinates(block, code)
    newlines = [m.start() for m in re.finditer('\n', clean)]
    def source_line(offset):
        return origin + bisect_left(newlines, offset)
    if re.search(r'\b(?:JMP|GOTO|ASM)\b', clean, re.I):
        for data in facts.values():
            data['open'].add(origin)
    available, _ = isolate_counted_loops(safe_code, writer_locations=True)
    parser = STParser(available, condition_parser=source_condition, strict=False, retain_source=True)
    try:
        parser.seq()
    except ERRORS:
        pass  # Every actual usage is checked below, including after an unsupported region.
    if cited_lines:
        guarded = {offset: guard for _, _, guard, offset, _ in parser.statements}
        guarded.update(parser.call_guards)
        for offset, guard in guarded.items():
            line = source_line(offset)
            if line in cited_lines:
                instruction = clean[offset:].partition('\n')[0].partition(';')
                for name, _ in constrained_states(guard, wanted):
                    controlled[name, line].add(instruction[0] + instruction[1])
    # Propagate only a single unconditional call of a declared IEC timer/edge in
    # the same ST section. A writable reference to the instance or its Q breaks it.
    clocks = {name: kind for name, kind in types.items() if kind in ('TON', 'R_TRIG')}
    for match in re.finditer(r'(?<![\w.])([^\W\d]\w*)(?:\s*\.\s*([^\W\d]\w*))?', clean):
        instance = match[1].casefold()
        if instance not in clocks and types.get(instance) != 'BOOL':
            continue
        after = clean[match.end():]
        before = clean[max(0, match.start()-160):match.start()]
        if instance not in clocks:
            if (re.search(r'(?:=>|REF\s*=|\bADR\s*\()\s*$', before, re.I) or
                    re.search(r'(?::=|\(|,)\s*$', before) and re.match(r'\s*[,)]', after)):
                flags[block].append((instance, 'OPAQUE', None, source_line(match.start())))
            continue
        port = instance + '.q'
        if match[2]:
            if re.match(r'\s*(?::=|REF=)', after, re.I) or re.search(r'(?:=>|REF\s*=|\bADR\s*\()\s*$', before, re.I):
                calls[port].append(None)
            elif re.match(r'\s*[,)]', after) and re.search(r'(?::=|\(|,)\s*$', before):
                pin = re.search(r'\b(\w+)\s*\(\s*(?:CLK|IN)\s*:=\s*$', before, re.I)
                if not pin or pin[1].casefold() not in clocks:
                    calls[port].append(None)
            continue
        opening = re.match(r'\s*\(', after)
        evidence = None
        if opening and parser.call_guards.get(match.start()) == TRUE:
            pin = 'IN' if clocks[instance] == 'TON' else 'CLK'
            args = [arg.split(':=', 1)[1] for arg in _args(clean, match.end() + opening.end())
                    if re.match(pin + r'\s*:=', arg, re.I)]
            if len(args) == 1:
                evidence = (source_condition(args[0]), source_line(match.start()), block, match.start())
        calls[port].append(evidence)
    parsed = {p:(l.casefold(),r,g) for l,r,g,p,_ in parser.statements}
    for lhs,rhs,guard,offset,_ in parser.statements:
        flags[block].append((lhs.casefold(), rhs.strip().upper(), guard, source_line(offset)))
    previous = {offset: (parser.ts[i-1].upper(), parser.spans[i-1][0])
                for i, (offset, _) in enumerate(parser.spans) if i and parser.ts[i].casefold() in wanted}
    dispatches = {(start, lhs) for offset, (lhs, _, _) in parsed.items()
                  for kind, start, _, selector in parser.frames.get(offset, ())
                  if kind == 'case' and selector.casefold() == lhs}
    audited = dispatch_operations(code, safe_code, parser, {lhs for _, lhs in dispatches if facts.get(lhs, {}).get('audit')}, shift)
    words = re.compile(r'(?<![\w.])(?:' + '|'.join(map(re.escape, wanted)) + r')(?!\w)', re.I)
    for m in words.finditer(clean):
        leaf = m[0].casefold()
        data = facts[leaf]
        pos = m.start()
        line = source_line(pos)
        after = clean[m.end():m.end()+80]
        if re.match(r'\s*:=', after):
            statement = parsed.get(pos)
            if statement and statement[0] == leaf:
                _, rhs, guard = statement
                data['writes'].append((integer(rhs), guard, line, block, pos, locator, shift))
                continue
        else:
            use = _read_use(parsed, dispatches, previous, leaf, clean, pos, m.end(), locator, shift)
            if use['kind'] == 'dispatch' and (audit := audited.get(use['offset'] - shift)):
                use['operations'] = audit
            data['reads'].append({'line': line, 'section_line': _section(block).sourceline, **locator, **use})
            if _read_only(clean[max(0,pos-180):pos], after):
                continue
        data['open'].add(line)


def _section(node):
    return next((p for p in node.iterancestors() if _tag(p) in ('bodycontent','body','rung')), node)


def _scalar_facts(unit, wanted, origins, project_types, cited_lines, controlled):
    types, initials, latches = {}, {}, set()
    for var in unit.iter('{*}Variable','{*}variable'):
        if not var.get('name') or not _tag(var.getparent()).endswith('vars'):
            continue
        leaf = var.get('name').casefold()
        kind = var.findtext('{*}Type/{*}TypeName', '') or next(
            (_tag(t) for t in var.findall('{*}type/*')), '')
        types[leaf] = kind.upper()
        if types[leaf] in ('TON', 'R_TRIG') and types[leaf].casefold() in project_types:
            types[leaf] = 'project_fb'
        if types[leaf] == 'BOOL' and _initial(var) == 0:
            latches.add(leaf)
        if leaf in wanted and types[leaf] in INTEGER_BOUNDS:
            value = _initial(var)
            initials[leaf] = None if leaf in initials else value
    private = {k:v for k,v in initials.items() if v is not None}
    if not private:
        return {}
    words = re.compile(r'(?<![\w.])(?:'+'|'.join(map(re.escape,private))+r')(?!\w)', re.I)
    def needs_audit(row):
        kinds = {s['kind'] for s in row['control_links'] if s['child'].casefold() == row['identity']}
        return {'starts_child', 'waits_for_child'} <= kinds
    facts = {leaf:{'open':set(), 'writes':[], 'reads':[], 'audit':any(map(needs_audit, wanted[leaf]))} for leaf in private}
    calls, flags = defaultdict(list), defaultdict(list)
    for body in unit.iter('{*}BodyContent','{*}body'):
        for node in body.iter():
            tag = _tag(node)
            if tag in ('bodycontent','body') or any(_tag(p) in ('st','stcode','comment','documentation')
                                                   for p in node.iterancestors() if p is not unit):
                continue
            if tag in ('st','stcode') and not len(node):
                text = ''.join(node.itertext())
                _block_facts(text, node, origins[node], private, types, facts, calls, flags, cited_lines, controlled)
            else:
                # A wired read/write, address alias or unsupported representation keeps the cursor open.
                for item in [*node.attrib.values(), ''.join(node.itertext()) if tag in ('st','stcode') else node.text or '', node.tail or '']:
                    for m in words.finditer(item):
                        facts[m[0].casefold()]['open'].add(node.sourceline)
                    for name in re.findall(r'[^\W\d]\w*', item.casefold()):
                        if name in latches:
                            flags[node].append((name, 'OPAQUE', None, node.sourceline))
                        if types.get(name) in ('TON', 'R_TRIG'):
                            calls[name + '.q'].append(None)
    result = {}
    for leaf, init in private.items():
        values = facts[leaf]
        resets, entries = [], []
        for number, guard, line, block, offset, locator, shift in values['writes']:
            if not compatible(guard,leaf,init,check_remaining=True):
                continue
            if number == init:
                resets.append(line)
            else:
                inputs = _required_inputs(guard, calls, block, offset)
                # The same activation that writes the cursor enables a run latch.
                # Losing the reviewed mode must independently suffice to stop that latch.
                starts = {n for n,v,g,_ in flags[block] if n in latches and v=='TRUE' and g==guard}
                starts.difference_update(n for section in flags.values() for n,v,g,_ in section
                                         if v != 'FALSE' and (v != 'TRUE' or g != guard))
                stops = {}
                for gate in inputs:
                    absent = negate(Expr('signal',(gate,)))
                    for name,value,cond,stop_line in flags[block]:
                        if (name in starts and value=='FALSE' and
                                (gate,False) in _all_polarities(cond) and
                                not compatible(combine('and',absent,negate(cond)),'',0,check_remaining=True)):
                            operations=[{'line':ln, 'target':output} for output,val,g,ln in flags[block] if output!=name and val=='TRUE'
                                        and (name,True) in required_signals(g)]
                            clocks = sorted(port.rsplit('.',1)[0] for port,sites in calls.items()
                                            if len(sites)==1 and sites[0] and sites[0][2] is block
                                            and sites[0][1] in inputs[gate])
                            stops[gate]={'latch':name,'line':stop_line,'enabled_operations':operations,'clocks':clocks}
                            break
                entries.append({'line':line, 'section_line':_section(block).sourceline, **locator, 'offset':offset + shift,
                                'value':number, 'guard':guard.text(), 'required_inputs':inputs,
                                **({'mode_stops':stops} if stops else {})})
        if values['writes']:
            result[leaf] = {'initial':init, 'reset_lines':sorted(set(resets)), 'entries':entries,
                            'open_lines':sorted(values['open']), 'read_uses':values['reads']}
    return result


def _all_polarities(condition):
    # A negative mode operand may be one of several sufficient ways to cancel a run.
    if condition.kind == 'or':
        return set().union(*(_all_polarities(part) for part in condition.args))
    return required_signals(condition)


class ExecutionEvidence:
    """Source excerpts stay in memory. Published records contain only the evidence actually used."""
    def __init__(self, source, inventory, scan, review):
        self.sections = defaultdict(list)
        self.declarations = scan.channel_declarations
        self.callees = defaultdict(set)
        self.controlled = defaultdict(lambda: defaultdict(set))
        by_owner = defaultdict(lambda: defaultdict(list))
        cited_lines = defaultdict(set)
        excerpts = set()
        decisions = review.get('decisions') if isinstance(review, dict) else None
        claims = {d['name'].casefold(): d['service_evidence'] for d in (decisions if isinstance(decisions, list) else ())
                  if isinstance(d, dict) and isinstance(d.get('name'), str) and isinstance(d.get('service_evidence'), dict)}
        for row in inventory['candidates']:
            for site in row.get('transitions', ()):
                for call in site.get('binding', ()):
                    self.callees[call['scope'], _owner(call['type'])].add(call['instance'].casefold())
            if row['eligibility'] not in ('scope_required', 'single_write') or (storage := _storage(row)) is None:
                continue
            if storage in scan.private_storage:
                owner, leaf = storage
                by_owner[owner][leaf].append(row)
                if claim := claims.get(row['identity']):
                    operation = claim.get('operation')
                    if isinstance(operation, dict) and type(operation.get('line')) is int:
                        cited_lines[owner].add(operation['line'])
                    for site in row['transitions']:
                        excerpts.add(_owner(site['program']))
                        excerpts.update(_owner(c['type']) for c in site.get('binding', ()))
        if not by_owner:
            return
        uses = defaultdict(set)
        for (_, unit), (name, _, _) in scan.references.items():
            if unit:
                uses[_owner(unit[1])].add(name.casefold())
            if '.' in name:
                uses[None].add(name.casefold())
        for owner, operands in scan.wired_operands.items():
            uses[owner].update(operands)
            uses[None].update(p for p in operands if '.' in p)
        members = {owner: {path[:m.start()] for path in paths for m in re.finditer(r'[.\[]', path)}
                   for owner, paths in uses.items()}
        def exposed(site, leaf):
            chain = site.get('binding', ())
            host = _owner(site['program'])
            parts = [host, *(c['instance'].casefold() for c in chain), leaf]
            callers = [host, *(_owner(c['type']) for c in chain[:-1])]
            scopes = [(None, 0), *((caller, i) for i, caller in enumerate(callers[:len(chain)], 1))]
            return any('.'.join(parts[i:end]) in uses.get(caller, ())
                       for caller,i in scopes for end in range(max(i+1, 2), len(parts)+1)) or any(
                       '.'.join(parts[i:]) in members.get(caller, ()) for caller,i in scopes)
        self.exposed = exposed
        # libxml can report the end of a multiline CDATA start tag. Use actual XML start
        # events for physical line numbers, including compact, namespaced or UTF-16 exports.
        positions = []
        locator = expat.ParserCreate(namespace_separator='}')
        def start(name, attrs):
            if name.rsplit('}',1)[-1] in ('ST','STCode'):
                positions.append(locator.CurrentLineNumber)
        locator.StartElementHandler = start
        with source.open('rb') as handle:
            locator.ParseFile(handle)
        positions = iter(positions)
        origins = {}
        for _,unit in etree.iterparse(str(source), events=('end',),
                                       tag=('{*}Program','{*}FunctionBlock','{*}Function','{*}pou','{*}ST','{*}STCode'), huge_tree=True,
                                       no_network=True, resolve_entities=False):
            if _tag(unit) in ('st','stcode'):
                origins[unit] = next(positions)
                continue
            owner = unit.get('name','').casefold()
            if owner in excerpts:
                for node in unit.iter('{*}ST','{*}STCode','{*}Rung'):
                    body = _section(node)
                    if _tag(node)=='rung':
                        notes=[(c.sourceline, ''.join(c.itertext()).strip()) for c in node.iter('{*}Content')
                               if any(any(v.rsplit(':',1)[-1].lower()=='comment' for v in p.attrib.values())
                                      for p in c.iterancestors() if p is not unit)]
                        end=max((c.sourceline for c in node.iter() if c.sourceline),default=node.sourceline)
                        self.sections[owner].append((node.sourceline,end,notes,[]))
                    else:
                        from plc_trace.capture.extract_step_variables import _executable_text
                        raw=''.join(node.itertext())
                        executable=''.join(_executable_text(raw, keep_positions=True)).split('\n')
                        without_comments=''.join(_executable_text(raw, keep_positions=True, keep_strings=True)).split('\n')
                        origin=origins[node]
                        self.sections[owner].append((body.sourceline,origin+raw.count('\n'),
                            [(i, ''.join(ch if mask==' ' else ' ' for ch,mask in zip(line,masked)))
                             for i,(line,masked) in enumerate(zip(raw.split('\n'),without_comments),origin)],
                            list(enumerate(executable,origin))))
            if owner in by_owner:
                facts_by_leaf = _scalar_facts(unit, by_owner[owner], origins, scan.type_bodies,
                                             cited_lines[owner], self.controlled[owner])
                for leaf, facts in facts_by_leaf.items():
                    for row in by_owner[owner][leaf]:
                        site = row['transitions'][0]
                        chain = site.get('binding', ())
                        parents = [_owner(site['program']), *(_owner(c['type']) for c in chain[:-1])]
                        if exposed(site, leaf) or any((caller, c['instance'].casefold()) not in scan.default_instances
                                                       for caller,c in zip(parents,chain)):
                            continue
                        row['activation']=facts
            for n in unit.iter('{*}ST','{*}STCode'):
                origins.pop(n,None)
            unit.clear()
            while unit.getprevious() is not None:
                del unit.getparent()[0]

    def cited_service(self, row, evidence):
        if not isinstance(evidence,dict) or not row.get('activation') or row['control_links']:
            return None
        kind=evidence.get('kind')
        if kind not in ('mode','equipment'):
            return None
        sites = row['transitions']
        bindings = {(t['scope'],tuple((c['scope'],c['instance'],c['source_line']) for c in t.get('binding',()))) for t in sites}
        if len(bindings)!=1:
            return None
        scope,chain=bindings.pop()
        host = _owner(sites[0]['program'])
        implementation = _owner(sites[0]['binding'][-1]['type']) if chain else host
        # A comment about the actual call and an executable operation in the cursor's own
        # body must both exist. The semantic classification is the project reviewer's claim.
        def excerpt(field, owner, base, executable):
            cite=evidence.get(field)
            if not isinstance(cite,dict) or type(cite.get('line')) is not int or not isinstance(cite.get('text'),str):
                return False
            needle=cite['text'].strip()
            if len(needle)<3:
                return False
            for first,last,raw,code in self.sections.get(owner,()):
                if first<=base<=last and first<=cite['line']<=last:
                    texts=dict(code if executable else raw)
                    item=texts.get(cite['line'],'')
                    if needle in unescape(item) and (not executable or re.search(r':=|\b\w+\s*\(', needle)):
                        return True
            return False
        base=chain[-1][2] if chain else sites[0]['source_line']
        caller = _owner(sites[0]['binding'][-2]['type']) if len(chain)>1 else host
        bases={s['source_line'] for s in sites}
        if not any(excerpt('operation',implementation,line,True) for line in bases):
            return None
        operation = evidence['operation']
        leaf = row['identity'].rsplit('.', 1)[-1]
        guarded = self.controlled[implementation][leaf, operation['line']]
        if not any(instruction.startswith(operation['text'].strip()) for instruction in guarded):
            return None
        proof = {'kind':kind, 'purpose':evidence.get('purpose'), 'operation':operation, 'scope':scope}
        if kind=='equipment':
            if (not row['activation']['open_lines'] and
                    all(c['guard']=='TRUE' for c in sites[0].get('binding', ())) and
                    (not chain or len(self.callees[chain[-1][0], implementation])==1) and
                    excerpt('purpose',caller,base,False)):
                return proof
        else:
            gate=evidence.get('gate','')
            act=row['activation']
            assigned=re.match(rf'({IDENT})\s*:=\s*TRUE\b',operation['text'].strip(),re.I)
            target={'line':operation['line'],'target':re.sub(r'\s+','',assigned[1]).casefold()} if assigned else None
            if (isinstance(gate,str) and gate and not act['open_lines'] and act['entries']
                    and target and all((stop := entry.get('mode_stops',{}).get(gate.casefold()))
                            and target in stop['enabled_operations'] and
                            not any(self.exposed(sites[0],name) for name in (stop['latch'], *stop['clocks']))
                            for entry in act['entries'])
                    and any(excerpt('purpose',implementation,line,False) for line in bases)):
                return {**proof,'gate':gate,'entry_lines':[e['line'] for e in act['entries']]}
        return None
