"""Source-only candidate ledger, scope decisions and project-local selector replay."""
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

from plc_trace.analysis.boundary_logic import IDENT, symbol
from plc_trace.analysis.candidate_filter import glob_regex
from plc_trace.analysis.successor_graph import _st_tokens_with_spans


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def fixed_path(value):
    return (bool(re.fullmatch(IDENT, value)) and
            all(re.fullmatch(r'-?\d+(?:,-?\d+)*', ix) for ix in re.findall(r'\[([^]]*)\]', value)))


def isolate_counted_loops(code):
    """Omit balanced assignment-only FORs, never transfers or calls. Keep original offsets.

    The caller ignores transitions of any root the omitted code may write.
    Conditions depending on a loop result are still conditions, not evaluated facts.
    """
    tokens = list(_st_tokens_with_spans(code))
    spans, touched = [], set()
    depth = 0
    for i, (token, (start, end)) in enumerate(tokens):
        upper = token.upper()
        if upper == 'FOR':
            if depth == 0:
                begin = start
            depth += 1
        if depth and (upper in {'RETURN', 'EXIT', 'CONTINUE', 'JMP', 'GOTO', 'REF=', '^'} or '^' in token):
            return code, set()
        if depth and token == '(' and i and re.fullmatch(IDENT, tokens[i-1][0]) and tokens[i-1][0].upper() not in {
                'IF', 'ELSIF', 'NOT', 'AND', 'OR', 'XOR', 'WHILE', 'DO', 'THEN'}:
            return code, set()
        if depth and token == ':=':
            lhs = symbol(tokens[i-1][0])
            if not re.fullmatch(IDENT, lhs):
                return code, set()
            touched.add(re.split(r'[.\[]', lhs, 1)[0].casefold())
        if upper == 'END_FOR':
            depth -= 1
            if depth < 0:
                return code, set()
            if not depth:
                spans.append((begin, end))
    if depth or not spans:
        return code, set()
    pieces, last = [], 0
    for start, end in spans:
        pieces.extend((code[last:start], re.sub(r'[^\r\n]', ' ', code[start:end])))
        last = end
    return ''.join(pieces) + code[last:], touched


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
    declared: str = ''
    type_issue: str = ''

    def edge(self, a, b, site):
        if a is None or b is None or a == b:
            return
        self.edges.add((a, b))
        # Keep distinct routines, instance bindings and branches available for scope review.
        site = {**site, 'from': a, 'to': b}
        key = site['scope'], a, b, site.get('guard', '')
        old = self.sites.get(key)
        if old is None or (site.get('st_line') and not old.get('st_line')):
            self.sites[key] = site

    def effect(self, step, site):
        if site.get('target') and site['target'].casefold() != self.name.casefold():
            self.effects.setdefault((site['scope'], step, site['target']), {**site, 'step': step})

    def record(self):
        starts = {a for a, _ in self.edges}
        ends = {b for _, b in self.edges}
        scoped_steps = {(site['scope'], site['from']) for site in self.sites.values()}
        effects = [site for (scope, step, _), site in self.effects.items() if (scope, step) in scoped_steps]
        # A one-way scalar reset (timer wrap, range clamp, recipe default) is not
        # a sequence. A short sequence can qualify through a real controlled output.
        chain = bool(starts & ends)
        if not self.edges:
            self.reasons.add('no_constrained_state_change')
        elif (not chain and not effects and not self.links and not
              any(s.get('case_selector') and s.get('other_condition') for s in self.sites.values())):
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
        links = [{**self.links[key], 'id': f'l{n}'} for n, key in enumerate(sorted(self.links), 1)]
        return {'name': self.name, 'identity': self.name.casefold(), 'type': self.declared or None,
                'type_issue': self.type_issue, 'eligibility': 'review' if self.reasons else 'scope_required',
                'reasons': sorted(self.reasons), 'edges': len(self.edges), 'scopes': sorted({s['scope'] for s in sites}),
                'transitions': sites, 'effects': effects, 'control_links': links,
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
            key = (parent, step, child, value, role, site['scope'])
            first.links.setdefault(key, link)
            second.links.setdefault(key, link)

    def add_unproven_siblings(self, scan):
        # Show observed lookalikes separately (e.g. a sibling that is only cleared).
        def shape(name):
            return re.sub(r'\d+', '#', name.casefold())
        families = {shape(row.name) for row in self.candidates.values() if row.edges}
        for (_, unit), (name, raw, line) in scan.references.items():
            if not any(c.isdigit() for c in name):
                continue
            if scan.source_format == 'omron_iec61131_10':
                name = scan.sysmac_channel(name, unit)[0]
            self.visible.add(name)
            if shape(name) in families:
                key = name.casefold()
                if key not in self.candidates:
                    self.candidate(name).reasons.add('reference_only')
                if not self.candidates[key].edges:
                    self.reference_sites.setdefault(key, {'spelling': raw, 'unit': unit, 'source_line': line})

    def inventory(self, source_sha, adapter):
        self.resolve_links()
        records = [self.candidates[k].record() for k in sorted(self.candidates)]
        data = {'schema': 'plc.step_discovery.v1', 'source_sha256': source_sha, 'adapter': adapter,
                'candidates': records, 'parse_limits': self.issues}
        data['inventory_sha256'] = digest(data)
        return data


_CAPTURE_ROLES = {'production', 'production_child', 'mixed'}
_ROLES = _CAPTURE_ROLES | {'auxiliary', 'uncertain'}


def scope_selection(inventory, decisions):
    """Admit only proven, scoped names. A reviewer cannot add names, types or transitions."""
    eligible = {r['identity']: r for r in inventory['candidates'] if r['eligibility'] == 'scope_required'}
    if decisions is None:
        return {}, []
    if not isinstance(decisions, dict) or decisions.get('inventory_sha256') != inventory['inventory_sha256']:
        raise ValueError('用途判断与本轮源码证据指纹不一致')
    rows = decisions.get('decisions')
    if not isinstance(rows, list):
        raise ValueError('用途判断须包含 decisions 数组')
    chosen, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
            raise ValueError('用途判断缺少变量名')
        name = row['name'].casefold()
        if name not in eligible or name in seen:
            raise ValueError(f'重复或无独立迁移证据的变量：{name}')
        seen.add(name)
        candidate = eligible[name]
        refs = {s['id'] for s in (*candidate['transitions'], *candidate['effects'], *candidate['control_links'])}
        citations = row.get('basis')
        if (not isinstance(row.get('role'), str) or row['role'] not in _ROLES or not isinstance(row.get('reason'), str) or not row['reason'].strip()
                or not isinstance(citations, list) or not citations
                or not all(isinstance(i, str) and i in refs for i in citations)):
            raise ValueError(f'用途、理由或本变量源码证据引用无效：{name}')
        if row['role'] in _CAPTURE_ROLES:
            if not any(i.startswith('t') for i in citations):
                raise ValueError(f'采纳变量须引用其本身的迁移：{name}')
            chosen[name] = candidate
    if seen != eligible.keys():
        raise ValueError(f'本轮仍有 {len(eligible.keys() - seen)} 个候选缺用途判断')
    return chosen, sorted(rows, key=lambda r: r['name'].casefold())


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
        if row['eligibility'] != 'scope_required':
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
                    brief = {k: site[k] for k in ('id', 'scope', 'source_line', 'st_line', 'kind', 'from', 'to',
                             'target', 'child', 'parent', 'comment', 'guard') if site.get(k) is not None}
                    if site.get('binding'):
                        brief['call'] = [{k: x[k] for k in ('scope', 'instance', 'source_line')} for x in site['binding']]
                    examples.append(brief)
        yield {'name': row['name'], 'type_issue': row['type_issue'],
               'scheduled_writers': row.get('scheduled_writers', {}), 'evidence': examples}
