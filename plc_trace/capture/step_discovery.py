"""Source-only candidate ledger, scope decisions and project-local selector replay."""
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

from plc_trace.analysis.boundary_logic import IDENT, symbol
from plc_trace.analysis.candidate_filter import glob_regex
from plc_trace.analysis.successor_graph import _st_tokens_with_spans
from plc_trace.capture.capture_use import timing_use
from plc_trace.capture.coverage import protect_channels
from plc_trace.capture.scope_recheck import verify_private_boundaries
from plc_trace.capture.station_scope import dependent_writes, family, required_cursors, same_station_array, writer_context
from plc_trace.source.source_expressions import source_condition


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def fixed_path(value):
    return (bool(re.fullmatch(IDENT, value)) and
            all(re.fullmatch(r'-?\d+(?:,-?\d+)*', ix) for ix in re.findall(r'\[([^]]*)\]', value)))


def _storage_region(path):
    """Known address prefix, up to the first data-dependent subscript."""
    path = symbol(path).casefold()
    dynamic = re.search(r'\[(?!-?\d+(?:,-?\d+)*\])', path)
    prefix = path[:dynamic.start()] if dynamic else path
    return re.sub(r'\[(-?\d+(?:,-?\d+)*)\]',
                  lambda m: ''.join(f'[{int(i)}]' for i in m[1].split(',')), prefix)


def loop_may_write(path, regions):
    """Whole-object assignments and dynamic indices cover descendants, sibling members stay distinct."""
    if not regions:
        return False
    path = _storage_region(path)
    return any(path == r or path.startswith((r + '.', r + '[')) or r.startswith((path + '.', path + '['))
               for r in regions)


def _sequence_witness(sites):
    """Locate an own CASE arm or two successive dispatches at the same concrete writer."""
    for site in sites:
        if site.get('case_selector'):
            return {'kind': 'case_dispatch', 'basis': [site['id']]}
    starts = {(writer_context(site), site['from']): site for site in reversed(sites)}
    for site in sites:
        successor = starts.get((writer_context(site), site['to']))
        if successor:
            return {'kind': 'successive_dispatch', 'basis': [site['id'], successor['id']]}
    return None


def isolate_counted_loops(code, *, writer_locations=False, allow_early_exit=True, trigger_type=None, omissions=None):
    """Omit balanced counted loops with only local control and assignments. Preserve offsets.

    The caller ignores transitions overlapping any storage the omitted code may write.
    Conditions depending on a loop result are still conditions, not evaluated facts.
    """
    tokens = list(_st_tokens_with_spans(code))
    spans, touched, local = [], set(), set()
    depth, skip_until = 0, -1
    for i, (token, (start, end)) in enumerate(tokens):
        if i < skip_until:
            continue
        upper = token.upper()
        if upper == 'FOR':
            if depth == 0:
                begin, local = start, set()
            depth += 1
        if depth and upper in {'RETURN','JMP','GOTO'}:
            return code, set()
        # EXIT and CONTINUE inside a FOR cannot leave the enclosing POU.
        # A timing path still needs the branch through the loop; discovery only needs an accessible write.
        if depth and upper in {'EXIT', 'CONTINUE'} and not (allow_early_exit or writer_locations):
            return code, set()
        if depth and not writer_locations and (upper in {'REF', 'REF='} or '^' in token):
            return code, set()
        previous = tokens[i-1][0] if i else ''
        if depth and not writer_locations and token == '(' and (re.fullmatch(IDENT, previous) or previous.endswith(']')) and previous.upper() not in {
                'IF', 'ELSIF', 'NOT', 'AND', 'OR', 'XOR', 'WHILE', 'DO', 'THEN'}:
            # A standard edge detector with one input pin writes its own FB
            # memory. Unknown calls, output mappings and computed arguments
            # cannot be omitted as local writes.
            loop = re.match(r'FOR\s+([^\W\d]\w*)\s*:=\s*(-?\d+)\s+TO\s+(-?\d+)\s+DO\b',
                            code[begin:], re.I)
            instance = tokens[i-1][0]
            if (not trigger_type or depth != 1 or not loop or i < 2
                    or tokens[i-2][0].upper() not in ('DO', 'THEN', 'ELSE', ';')
                    or i + 5 >= len(tokens)
                    or [item[0].upper() for item in tokens[i+1:i+3]] != ['CLK', ':=']
                    or not re.fullmatch(IDENT, tokens[i+3][0])
                    or [item[0] for item in tokens[i+4:i+6]] != [')', ';']
                    or not trigger_type(instance, *loop.groups())):
                return code, set()
            local.add(_storage_region(instance))
            skip_until = i + 5
        if depth and token == ':=' and not writer_locations:
            # The ST tokenizer separates commas in multidimensional subscripts.
            # Validate the whole lvalue, including its containing structure.
            first = i - 1
            while first and tokens[first - 1][0].upper() not in {'FOR', 'DO', 'THEN', 'ELSE', ';', ':'}:
                first -= 1
            lhs = symbol(code[tokens[first][1][0]:start])
            if not re.fullmatch(IDENT, lhs):
                return code, set()
            local.add(_storage_region(lhs))
        if upper == 'END_FOR':
            depth -= 1
            if depth < 0:
                return code, set()
            if not depth:
                spans.append((begin, end, local))
                touched.update(local)
    if depth or not spans:
        return code, set()
    pieces, last = [], 0
    if omissions is not None:
        omissions.extend((start, writes) for start, _, writes in spans)
    for start, end, _ in spans:
        pieces.extend((code[last:start], re.sub(r'[^\r\n]', ' ', code[start:end])))
        last = end
    return ''.join(pieces) + code[last:], touched


def gated_write(site):
    return site.get('guard_complete') and site.get('guard', '').upper() not in ('', 'TRUE', 'FALSE')


@dataclass
class Candidate:
    name: str
    sites: dict = field(default_factory=dict)
    effects: dict = field(default_factory=dict)
    edges: set = field(default_factory=set)
    links: dict = field(default_factory=dict)
    reasons: set = field(default_factory=set)
    owners: set = field(default_factory=set)
    scheduled: dict = field(default_factory=dict)
    declarations: list = field(default_factory=list)
    declared: str = ''
    type_issue: str = ''
    request_inputs: set = field(default_factory=set)
    other_outputs: set = field(default_factory=set)
    service_isolation: dict | None = None
    dispatches: dict = field(default_factory=dict)
    external_writes: dict = field(default_factory=dict)

    def external_write(self, value, site):
        known = self.external_writes.get(value)
        if known is None or (gated_write(site) and not gated_write(known)):
            self.external_writes[value] = site

    def dispatch(self, step, site, selector, source=None):
        """A read selecting an action, independent of whether that action writes this cursor."""
        source = source or site
        cases = [frame[3].casefold() for frame in source.get('frames', ()) if frame[0] == 'case']
        if cases:
            if cases[-1] != selector:
                return
        elif required_cursors(source_condition(source.get('guard', ''))) != {selector}:
            return
        self.dispatches.setdefault(step, site)

    def edge(self, a, b, site):
        if a is None or b is None or a == b:
            return
        self.edges.add((a, b))
        # Keep distinct routines, instance bindings and branches available for scope review.
        site = {**site, 'from': a, 'to': b}
        key = writer_context(site), a, b, site.get('guard', '')
        old = self.sites.get(key)
        if old is None or (site.get('st_line') and not old.get('st_line')):
            self.sites[key] = site

    def effect(self, step, site):
        if site.get('target') and site['target'].casefold() != self.name.casefold():
            key = writer_context(site), step, site['target'], site.get('value'), site.get('network'), site.get('guard', '')
            self.effects.setdefault(key, {**site, 'step': step})

    def record(self):
        starts = {a for a, _ in self.edges}
        ends = {b for _, b in self.edges}
        scoped_steps = {(writer_context(site), site['from']) for site in self.sites.values()}
        effects = [site for key, site in self.effects.items() if key[:2] in scoped_steps]
        # A one-way scalar reset (timer wrap, range clamp, recipe default) is not
        # a sequence. A short sequence can qualify through a real controlled output.
        chain = bool(starts & ends)
        if not self.edges:
            self.reasons.add('no_constrained_state_change')
        elif not (chain or effects or self.links or any(s.get('case_selector') for s in self.sites.values())):
            self.reasons.add('isolated_write_without_controlled_effect')
        if not fixed_path(self.name):
            self.reasons.add('nonconstant_or_unaddressable_reference')
        def location(s):
            return (s['scope'], s.get('source_line', 0), s.get('st_line', 0), s.get('from', 0),
                    s.get('to', 0), s.get('target', ''), s.get('guard', ''))
        sites = sorted(self.sites.values(), key=location)
        effects.sort(key=location)
        for n, site in enumerate(sites, 1):
            site['id'] = f't{n}'
        for n, site in enumerate(effects, 1):
            site['id'] = f'e{n}'
        links = [{**self.links[key], 'id': f'l{n}'} for n, key in enumerate(sorted(self.links, key=repr), 1)]
        eligibility = ('single_write' if self.reasons == {'isolated_write_without_controlled_effect'}
                       else 'review' if self.reasons else 'scope_required')
        separate = self.dispatches.keys() & self.external_writes.keys()
        split = (not sites and len(separate) >= 2 and fixed_path(self.name)
                 and not (self.reasons - {'no_constrained_state_change', 'reference_only'}))
        if split:
            eligibility = 'distributed_write'
            self.reasons.discard('no_constrained_state_change')
        entries = [{'value': n, **self.external_writes[n]} for n in sorted(starts & self.external_writes.keys())
                   if gated_write(self.external_writes[n])]
        scopes = {s['scope'] for s in sites}
        if split:
            scopes.update(s['scope'] for n in separate for s in (self.dispatches[n], self.external_writes[n]))
        return {'name': self.name, 'identity': self.name.casefold(), 'type': self.declared or None,
                'family': family(self.name),
                'declarations': [{**d, 'id': f'd{i}'} for i, d in enumerate(self.declarations, 1)],
                'type_issue': self.type_issue, 'eligibility': eligibility,
                'reasons': sorted(self.reasons), 'edges': len(self.edges), 'scopes': sorted(scopes),
                'transitions': sites, 'effects': effects, 'control_links': links,
                **({'entry_writes': entries} if eligibility == 'single_write' and entries else {}),
                **({'dispatch_writes': [{'value': n, 'dispatch': self.dispatches[n], 'write': self.external_writes[n]}
                                        for n in sorted(separate)]} if split else {}),
                'sequence_witness': _sequence_witness(sites),
                **({'request_inputs': sorted(self.request_inputs)} if self.request_inputs else {}),
                **({'other_outputs': sorted(self.other_outputs)} if self.other_outputs else {}),
                **({'service_isolation': self.service_isolation} if self.service_isolation else {}),
                **({'scheduled_writers': self.scheduled} if self.scheduled else {})}


class Discovery:
    def __init__(self):
        self.candidates = {}
        self.issues = {}
        self.visible = set()
        self.reference_sites = {}
        self.possible_links = []

    def candidate(self, name):
        key = name.casefold()
        if key not in self.candidates:
            self.candidates[key] = Candidate(name)
        return self.candidates[key]

    def resolve_links(self):
        for parent, step, child, value, role, site in self.possible_links:
            first, second = self.candidates.get(parent.casefold()), self.candidates.get(child.casefold())
            if first is None or second is None or first is second:
                continue
            if not any(a == step for a, _ in first.edges):
                continue
            values = {a if role == 'starts_child' else b for a, b in second.edges}
            if value not in values:
                continue
            link = {**site, 'parent': first.name, 'parent_step': step, 'child': second.name,
                    'child_value': value, 'kind': role}
            key = (parent, step, child, value, role, writer_context(site), site.get('guard', ''))
            first.links.setdefault(key, link)
            second.links.setdefault(key, link)

    def add_unproven_siblings(self, scan):
        # Show observed lookalikes separately (e.g. a sibling that is only cleared).
        families = {family(row.name) for row in self.candidates.values() if row.edges}
        for (_, unit), (name, raw, line) in scan.references.items():
            if not any(c.isdigit() for c in name):
                continue
            if scan.source_format == 'omron_iec61131_10':
                name = scan.sysmac_channel(name, unit)[0]
            self.visible.add(name)
            if family(name) in families:
                key = name.casefold()
                if key not in self.candidates:
                    self.candidate(name).reasons.add('reference_only')
                if not self.candidates[key].edges:
                    self.reference_sites.setdefault(key, {'spelling': raw, 'unit': unit, 'source_line': line})

    def inventory(self, source_sha, adapter):
        self.resolve_links()
        records = [self.candidates[k].record() for k in sorted(self.candidates)]
        controllers = {r['identity'] for r in records if r['eligibility'] == 'scope_required'}
        for row in records:
            for site in row['transitions']:
                if 'co_cursors' in site:
                    site['co_cursors'] = sorted(controllers.intersection(site['co_cursors']) - {row['identity']})
        data = {'schema': 'plc.step_discovery.v5', 'scope_policy': 'production_timing', 'source_sha256': source_sha, 'adapter': adapter,
                'candidates': records, 'parse_limits': self.issues}
        data['inventory_sha256'] = digest(data)
        return data


_ROLES = {'station_auto', 'station_slot', 'component_sequence', 'auxiliary_sequence', 'auxiliary', 'non_sequence', 'uncertain'}


def _station_evidence(candidate, row, citations):
    scope = row.get('owner_scope')
    own = [s for s in candidate['transitions'] if s['scope'] == scope and s['id'] in citations]
    if not own:
        raise ValueError(f'工位采纳项须引用该生产例程内本通道的迁移：{candidate["name"]}')
    declarations = candidate['declarations']
    writers = {writer_context(site) for site in own}
    if any(d['id'] not in citations for d in declarations):
        raise ValueError(f'工位采纳项须核对该实例、成员和下标的声明：{candidate["name"]}')
    corroborated = (any(d.get('text') for d in declarations) or any(s.get('binding') for s in own)
                    or any(writer_context(e) in writers and e['id'] in citations for e in candidate['effects'])
                    or len({s['from'] for s in own}) > 1)
    if not corroborated:
        raise ValueError(f'工位归属缺独立佐证，请补充本例程证据或标为待核：{candidate["name"]}')


def scope_selection(inventory, decisions, required_channels=(), *, execution=None):
    """Separate source evidence of progression from the channel's role in the machine."""
    eligible = {r['identity']: r for r in inventory['candidates'] if r['eligibility'] in {'scope_required', 'single_write'}}
    required = {key for key, row in eligible.items() if row['eligibility'] == 'scope_required'}
    selected = {key: row for key, row in eligible.items() if row['sequence_witness']}
    if decisions is None:
        return selected, []
    if not isinstance(decisions, dict) or decisions.get('inventory_sha256') != inventory['inventory_sha256']:
        raise ValueError('用途判断与本轮源码证据指纹不一致，请重新核对步序变量')
    rows, examples = decisions.get('decisions'), decisions.get('station_families', [])
    if not isinstance(rows, list) or not isinstance(examples, list):
        raise ValueError('工位范围判断须包含 station_families 与 decisions 数组')
    families = {}
    for example in examples:
        if not isinstance(example, dict) or not isinstance(example.get('example'), str):
            raise ValueError('工位变量族缺少本轮可核对的代表实例')
        key = example['example'].casefold()
        if key not in eligible or not isinstance(example.get('boundary'), str) or not example['boundary'].strip():
            raise ValueError('工位变量族须给出真实实例和独立周期的归属依据')
        group = eligible[key]['family']
        if group in families:
            raise ValueError(f'重复声明工位变量族：{group}')
        families[group] = key
    reviewed = {}
    distributed = {r['identity'] for r in inventory['candidates'] if r['eligibility'] == 'distributed_write'}
    replayed = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            raise ValueError('用途判断缺少变量名')
        key = row['name'].casefold()
        if key in distributed and key not in replayed and row.get('role') == 'uncertain':
            replayed.add(key)
            continue  # Reconstruct this source-only finding from the inventory.
        if key not in eligible or key in reviewed:
            raise ValueError(f'重复或无独立迁移证据的变量：{key}')
        candidate = eligible[key]
        refs = {site['id'] for field in ('transitions', 'effects', 'control_links', 'declarations')
                for site in candidate[field]}
        citations = row.get('basis')
        if (not isinstance(row.get('role'), str) or row['role'] not in _ROLES
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()
                or not isinstance(citations, list) or not citations
                or not all(isinstance(i, str) and i in refs for i in citations)):
            raise ValueError(f'用途、理由或本变量源码证据引用无效：{key}')
        # Recompute the output disposition even if the user replays a previous result.
        item = {k: row[k] for k in ('role', 'basis', 'reason')}
        item['name'] = candidate['name']
        transition_ids = {site['id'] for site in candidate['transitions']}
        if row['role'] in {'component_sequence', 'auxiliary_sequence', 'non_sequence'} and transition_ids.isdisjoint(citations):
            raise ValueError(f'确认执行步或排除数据通道须引用本实例的写步位置：{key}')
        if candidate['eligibility'] == 'single_write' and row['role'] in {'component_sequence', 'auxiliary_sequence'}:
            documented = any(d['id'] in citations and d.get('text') for d in candidate['declarations'])
            annotated = any(t['id'] in citations and t.get('comment') for t in candidate['transitions'])
            if not (documented or annotated):
                raise ValueError(f'单向写回须补充该实例的用途声明或写点注释：{key}')
        if row['role'] == 'non_sequence':
            # Both data codes and sequence cursors can have the same numeric graph.
            # Require source evidence of the alternative use beyond the candidate write.
            support = {site['id'] for site in candidate['effects'] + candidate['control_links']}
            support.update(d['id'] for d in candidate['declarations'] if d.get('text'))
            if support.isdisjoint(citations) and len(transition_ids.intersection(citations)) < 2:
                raise ValueError(f'排除非步序数据须补充声明说明、受控读写或另一写点：{key}')
            selected.pop(key, None)
        elif row['role'] in {'component_sequence', 'auxiliary_sequence'}:
            selected[key] = candidate
        if row['role'] in {'station_auto', 'station_slot'}:
            if candidate['family'] not in families:
                raise ValueError(f'该通道尚未证明所属工位自动步接口：{key}')
            _station_evidence(candidate, row, set(citations))
            selected[key] = candidate
            item['owner_scope'] = row['owner_scope']
            if row['role'] == 'station_slot':
                peer = row.get('interface_peer')
                if not isinstance(peer, str) or peer.casefold() == key:
                    raise ValueError(f'工位槽位须指向同一接口的另一已核实实例：{key}')
                item['interface_peer'] = peer
        if row['role'] == 'non_sequence':
            if row.get('boundaries') or row.get('use') not in (None, 'unresolved'):
                raise ValueError(f'非步序数据不能声明采集用途：{key}')
            item['admission'] = 'excluded'
        else:
            item.update(timing_use(candidate, row, key in selected))
        if row.get('service_evidence') is not None:
            if not isinstance(row['service_evidence'], dict) or item.get('use') != 'service':
                raise ValueError(f'服务依据须属于该实例的服务用途判断：{key}')
            item['service_evidence'] = row['service_evidence']
        if row.get('dedicated_step_array'):
            declarations = [s for s in candidate['declarations'] if s.get('array_bounds') and s.get('text')
                            and s['path'].casefold() == key and s['id'] in citations]
            if (key not in selected or len(declarations) != 1 or not re.search(r'\[-?\d+\]$', key)
                    or transition_ids.isdisjoint(citations)):
                raise ValueError(f'步序专用数组须核实本实例的叶数组声明、边界及写点：{key}')
            item['dedicated_step_array'] = True
        reviewed[key] = item
    if missing := required - reviewed.keys():
        raise ValueError(f'本轮仍有 {len(missing)} 个候选缺用途判断')
    if any(reviewed.get(key, {}).get('role') != 'station_auto' for key in families.values()):
        raise ValueError('工位变量族的代表实例也须有逐通道生产例程证据')
    for key, row in selected.items():
        decision = reviewed[key]
        if decision['role'] != 'station_slot':
            continue
        peer = decision['interface_peer'].casefold()
        if (peer not in selected or reviewed[peer]['role'] != 'station_auto'
                or not same_station_array(row, decision, selected[peer], reviewed[peer])):
            raise ValueError(f'无法证明该槽位与工位主实例的声明地址及生产程序一致：{key}')
    for key, parents in dependent_writes(selected).items():
        reviewed[key]['gated_by'] = parents
    roles = list(reviewed.values())
    protect_channels(inventory, roles, required_channels, execution=execution)
    if execution and execution.workstations.commands.owners:
        verify_private_boundaries(inventory, roles, execution.workstations.commands, required_channels)
    for decision in roles:
        key = decision['name'].casefold()
        if decision['admission'] == 'retained' and key in eligible:
            selected[key] = eligible[key]
    return selected, sorted(roles, key=lambda d: d['name'].casefold())


def array_coverage(inventory, selected, decisions, references):
    """Find concrete read/reset-only slots in a reviewed, bounded step array."""
    arrays, slots = {}, {}
    for decision in decisions:
        if not decision.get('dedicated_step_array'):
            continue
        row = selected[decision['name'].casefold()]
        base = row['name'].rsplit('[', 1)[0]
        decl = next(d for d in row['declarations'] if d.get('array_bounds') and d['path'].casefold() == row['identity'])
        arrays[base.casefold()] = {'array': base, 'bounds': decl['array_bounds'], 'declaration_line': decl['line'],
                                   'example': row['name'], 'type': row['type'], 'referenced_slots': []}
    for row in inventory['candidates']:
        key = row['identity']
        if key not in references or set(row['reasons']) != {'no_constrained_state_change', 'reference_only'}:
            continue
        match = re.fullmatch(r'(.+)\[(-?\d+)\]', key)
        group = arrays.get(match[1]) if match else None
        if group and group['bounds'][0] <= int(match[2]) <= group['bounds'][1] and group['type']:
            name = row['name']
            slots[key] = {'name': name, 'type': group['type'], 'source_line': references[key]['source_line'],
                          'evidence': 'referenced_step_slot', 'array_example': group['example']}
            group['referenced_slots'].append(name)
    return slots, list(arrays.values())


def replay_selectors(names, visible, explicit=None):
    """Compress varying numeric fields only if full matches reproduce the exact set."""
    families = defaultdict(list)
    for name in sorted(names, key=str.casefold):
        families[tuple(re.split(r'\d+', name.casefold()))].append(name)
    universe = {name.casefold() for name in visible} | {name.casefold() for name in names}
    literals = {n.casefold() for n in (explicit if explicit is not None else universe)}
    result = []
    for group in families.values():
        expected = {n.casefold() for n in group}
        tokens = [re.split(r'(\d+)', n) for n in group]
        rule = 're:' + ''.join(re.escape(field[0]) if len({s.casefold() for s in field}) == 1 else r'[0-9]+'
                                for field in zip(*tokens))
        matcher = glob_regex(rule)
        matches = {s for s in universe if matcher.fullmatch(s)}
        result.append({'selector': rule if len(group) > 1 and matches == expected else None,
                       'channels': group, 'other_matches': sorted(matches - expected),
                       'nonliteral_full_paths': [n for n in group if n.casefold() not in literals]})
    return result


def scope_index(inventory):
    """One line per eligible channel for bounded agent reads; full evidence stays in the ledger."""
    for row in inventory['candidates']:
        if row['eligibility'] == 'distributed_write':
            yield {k: row[k] for k in ('name', 'eligibility', 'dispatch_writes', 'declarations')}
            continue
        if row['eligibility'] not in {'scope_required', 'single_write'}:
            continue
        examples = []
        for field in ('transitions', 'effects', 'control_links'):
            by_scope = defaultdict(list)
            for item in row[field]:
                by_scope[item['scope']].append(item)
            for items in by_scope.values():
                # Spread excerpts over early/middle/late phases of each routine.
                for i in sorted({0, len(items)//2, len(items)-1}):
                    site = items[i]
                    brief = {k: site[k] for k in ('id', 'scope', 'program', 'source_line', 'st_line', 'kind', 'from', 'to',
                             'target', 'step', 'child', 'parent', 'comment', 'operating_guard', 'guard', 'co_cursors') if site.get(k) is not None}
                    if site.get('binding'):
                        brief['call'] = [{k: x[k] for k in ('scope', 'instance', 'source_line')} for x in site['binding']]
                    examples.append(brief)
        # A parent starting or reading this cursor matters even if its site is
        # outside the brief host excerpts above. IDs resolve in the full ledger.
        parents = defaultdict(lambda: defaultdict(list))
        for link in row['control_links']:
            if link['child'].casefold() == row['identity']:
                parents[link['parent']][link['kind']].append(link['id'])
        yield {'name': row['name'], 'family': row['family'], 'eligibility': row['eligibility'], 'declarations': row['declarations'],
               **({'parent_links': dict(parents)} if parents else {}),
               'type_issue': row['type_issue'], 'sequence_witness': row['sequence_witness'],
               'scheduled_writers': row.get('scheduled_writers', {}), 'evidence': examples,
               **({'activation': row['activation']} if 'activation' in row else {})}


def discover_source_writes(source, dialect, on_transition, on_unresolved=None, on_entry=None):
    """Use the capture adapters' typed instance binding and concrete write locations."""
    from plc_trace.capture.extract_step_variables import _SourceScan
    if dialect == 'plcopen':
        from plc_trace.capture.plcopen_discovery import discover
    else:
        from plc_trace.capture.sysmac_discovery import discover
    scan = _SourceScan(None, dialect, False)
    scan.read(source)
    return discover(source, scan, on_transition=on_transition, on_unresolved=on_unresolved, on_entry=on_entry)
