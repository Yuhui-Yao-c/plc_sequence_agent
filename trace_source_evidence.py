"""Link an observed trace pair to local PLC evidence without altering the jump."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path

from atomic_boundaries import PROVEN, atomic, stable_id
from trace_call_evidence import CallEvidence

EVIDENCE_FILE = 'trace_source_evidence.json'
RELATION_TEXT = {
    'direct': '源码直跳有证据',
    'direct_unverified': '源码直跳门控待核',
    'path_candidate': '源码多步路径待核',
    'path_via_zero_candidate': '源码经步0路径待核',
    'ambiguous_scope': '源码控制器作用域待核',
    'unresolved': '源码迁移未对齐',
}

# These are explicit instruction types. Argument names and signal names are
# kept for review, never used to infer the type of a physical operation.
_MOTION = {
    'axismove': '定位', 'mc_moveabsolute': '定位', 'mc_moverelative': '定位',
    'mc_movevelocity': '运行', 'onecycontrol': '气缸', 'twocycontrol': '气缸',
    'fourcycontrol': '气缸',
}
_COIL = {'coil_set': '置位', 'coil_reset': '复位', 'coil_write': '写入'}
_STEP_PREFIX = re.compile(r'^\s*(?:步\s*)?(\d+)\s*[-—:：>]\s*')
_TEMPLATE = re.compile(r'^相邻已观察|^源码局部状态迁移|^步进保持|^迁移（|^0状态已出现|^触发.*到达信号.*参与接收条件')
_NOISE_COMMENT = re.compile(r'^(计时开始|计时结束|计时)$')


def _read_csv(path):
    if path.is_file():
        with path.open(encoding='utf-8-sig', newline='') as stream:
            yield from csv.DictReader(stream)


def _text_rows(rows):
    for row in rows:
        yield {key: str(value) if value is not None else '' for key, value in row.items()}


def _state(label):
    value = atomic(label)
    if value is not None and value.kind == 'eq':
        variable, number = value.args
        return variable.casefold(), number
    return None


def _comment_text(comment, source_step):
    lines = comment.strip().splitlines()
    if not lines or _TEMPLATE.search(lines[0]) or _NOISE_COMMENT.fullmatch(lines[0].strip()):
        return ''
    text = lines[0].strip()
    prefix = _STEP_PREFIX.match(text)
    if prefix:
        if int(prefix[1]) != source_step:
            return ''
        text = text[prefix.end():].strip()
    return text if re.search(r'[\u4e00-\u9fff]', text) else ''


def _command_role(command):
    command = command.casefold()
    if command in _MOTION:
        return 'actuator_command', '机械动作', _MOTION[command]
    if command in _COIL:
        return 'signal_write', '信号/标志', _COIL[command]
    if command in {'ton', 'tof', 'tp'}:
        return 'timer', '工艺等待', '定时'
    return 'source_call', '', ''


def _path(adjacency, source, target, *, proved_only, allow_zero=False):
    pending = deque([source])
    previous = {source: None}
    while pending and target not in previous:
        current = pending.popleft()
        for edge in adjacency.get(current, ()):
            if proved_only and not edge['guard_complete']:
                continue
            dest = edge['to']
            if dest == 0 and dest != target and not allow_zero:
                continue
            if dest not in previous:
                previous[dest] = edge
                pending.append(dest)
    if target not in previous:
        return []
    edges = []
    while target != source:
        edge = previous[target]
        edges.append(edge)
        target = edge['from']
    return list(reversed(edges))


class TraceSourceIndex:
    def __init__(self, run, variables, *, source=None):
        self.transitions = defaultdict(list)
        self.graphs = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        self.bindings = defaultdict(set)
        self.commands = defaultdict(list)
        self.notes = defaultdict(list)
        self.station_scopes = defaultdict(Counter)
        self.call_evidence = CallEvidence()
        wanted = {name.casefold() for name in variables}
        run = Path(run)
        path, self.source_format, self.source_fingerprint = self._source_input(run, source)
        self.control = None
        self.audit = {}
        self.events = ()
        if self.source_format == 'plcopen':
            from plcopen_trace_control import collect
            self.control = collect(path, variables)
        if self.control is None:
            self._load_ledger(run, wanted, path)
        else:
            for variable, edge in self.control.edges:
                self._add_edge(variable, edge)
            self.commands.update(self.control.commands)
            from plcopen_trace_control import comment_text, step_comment_text
            network_states = defaultdict(set)
            for state, writes in self.control.state_writes.items():
                for write in writes:
                    network_states[write['body'], write['network']].add(state)
            for state, notes in self.control.notes.items():
                for note in notes:
                    single = len(network_states[note['body'], note['network']]) == 1
                    desc = step_comment_text(note['raw'], state[1], single_step=single)
                    record = {**note, 'text': desc or comment_text(note['raw']),
                              'applies_to_step': bool(desc), 'source_file': 'PLC XML'}
                    record['id'] = stable_id('TC', [state, record])
                    self.notes[state].append(record)
            self.program_names = defaultdict(set)
            for scopes in self.control.programs.values():
                for scope in scopes:
                    self.program_names[scope.split('::')[-1]].add(scope)
        from trace_description_context import DescriptionContext
        self.context = DescriptionContext(run, self, wanted)

    def station(self, variable):
        """Resolve a channel's display group from actual executable writers."""
        if self.control is None:
            return ''
        scopes = self.control.programs.get(variable.casefold(), set())
        if len(scopes) != 1:
            return variable
        scope, = scopes
        name = scope.split('::')[-1]
        return (name if len(self.program_names[name]) == 1 else scope) or variable

    @staticmethod
    def _source_input(run, source):
        from source_format import detect_source_format
        from workflow import fingerprint
        manifest_path = run / 'workflow_manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.is_file() else {}
        path = Path(source or manifest.get('source') or '')
        if path.is_file():
            digest = fingerprint(path)
            if manifest.get('source_sha256') and manifest['source_sha256'] != digest:
                raise ValueError('PLC 源码与本轮准备产物不一致，请重建输出目录')
            return path, detect_source_format(path), digest
        if manifest.get('source') or (run / 'plcopen_action_trace.csv').is_file():
            raise ValueError('Trace 描述需要本轮 PLC 源码')
        return None, '', ''

    def _load_ledger(self, run, wanted, source):
        model = None
        if self.source_format == 'omron_iec61131_10':
            from omron_native import collect_native
            model = collect_native(source)
        elif self.source_format == 'smc2':
            from smc2_controlflow import collect_smc2
            model = collect_smc2(source)

        if model is not None:
            from event_graph_v6 import FIELDS, smc2_events
            transitions = _text_rows({**r, 'path': ';'.join(r['path'])} for r in model['transitions'])
            stages = _text_rows(model['stage_records'])
            commands = _text_rows({**r, 'path': ';'.join(r['path'])} for r in model.get('native_commands', ()))
            events = model.get('native_events')
            if events is None:
                events = [*smc2_events(source), *model['stage_events']]
            self.events = _text_rows(dict(zip(FIELDS, event)) for event in events)
            self.audit = model.get('native_parse_audit') or model.get('smc2_audit', {})
        else:
            transitions = _read_csv(run / 'source_transition_edges.csv')
            stages = _read_csv(run / 'source_stage_candidates.csv')
            commands = _read_csv(run / 'native_command_evidence.csv')
            self.events = _read_csv(run / 'indexed_events.csv')

        edges_by_id = {}
        for row in transitions:
            variable = row['variable'].casefold()
            if variable not in wanted or not row['scope']:
                continue
            network, _, location = row['body'].partition(';')
            native = row['evidence'].startswith('omron_native_')
            edge = {
                'source_id': row['id'], 'scope': row['scope'], 'binding': row['binding_scope'],
                'from': int(row['from']), 'to': int(row['to']), 'guard': row['guard'],
                'guard_complete': row['guard_complete'] == 'True' and row['evidence'] in PROVEN,
                'grade': row['evidence'], 'body': row['body'], 'path': row['path'],
                'network': network if native else '', 'node': location if native else '',
                'source_line': '', 'comment': '',
            }
            self._add_edge(variable, edge)
            edges_by_id[row['id']] = (variable, edge)

        for row in stages:
            start = _state(row.get('开始标签'))
            end = _state(row.get('结束标签'))
            scope = row.get('证据POU', '')
            if start and start[0] in wanted:
                if scope:
                    self.station_scopes[start[0]][scope.split('::', 1)[0]] += 1
                self._note(start, row.get('动作描述', ''), scope, row.get('网络序号', ''),
                           row.get('证据节点', ''), 'PLC XML' if model is not None else 'source_stage_candidates.csv', 3)
            if not start or not end or start[0] not in wanted or start[0] != end[0] or not scope:
                continue
            source_id = row.get('证据节点', '')
            details = {
                'comment': row.get('动作描述', ''), 'network': row.get('网络序号', ''),
                'source_line': row.get('源码行号', ''),
            }
            if source_id in edges_by_id:
                variable, edge = edges_by_id[source_id]
                if (variable, edge['from'], edge['to'], edge['scope']) == (*start, end[1], scope):
                    edge.update(details)
            elif row.get('状态', '').endswith('_connected_transition_unverified'):
                self._add_edge(start[0], {
                    **details, 'source_id': source_id, 'scope': scope, 'node': source_id,
                    'binding': row.get('binding_scope', ''), 'from': start[1], 'to': end[1],
                    'guard': row.get('原始条件', ''), 'guard_complete': False,
                    'grade': row.get('证据等级', ''), 'body': '', 'path': '',
                })

        for row in commands:
            self.call_evidence.add(row)
            start = _state(row['start'])
            if start and start[0] in wanted:
                role, kind, operation = _command_role(row['command'])
                record = {
                    **row, 'role': role, 'kind': kind, 'operation': operation,
                    'guard_complete': row['guard_complete'] == 'True',
                }
                record['id'] = stable_id('TE', record)
                self.commands[start].append(record)
                self._note(start, row.get('comment', ''), row['scope'], row['network'],
                           row['node'], 'PLC XML' if model is not None else 'native_command_evidence.csv', 2)

        if model is not None:
            self._load_inventory(model['state_observations'], wanted)
        else:
            for row in _read_csv(run / 'indexed_candidates.csv'):
                start = _state(row.get('开始标签', ''))
                if start and start[0] in wanted and row.get('状态') != 'reset_hypothesis':
                    self._note(start, row.get('动作描述', ''), row.get('证据POU', ''), '',
                               row.get('证据节点', ''), 'indexed_candidates.csv', 1)

    def _load_inventory(self, records, wanted):
        observations = defaultdict(list)
        comparison_scopes = defaultdict(set)
        for row in records:
            variable = row['variable'].casefold()
            if variable not in wanted:
                continue
            observations[variable, row['value'], row.get('binding_scope', '')].append(row)
            if (self.source_format == 'omron_iec61131_10' and row['kind'] in ('comparison', 'st_comparison')
                    and row['operator'] == '=' and row['entry']):
                comparison_scopes[variable].add(row['entry'].split('::', 1)[0])

        # Native IEC inventory can identify the channel's POU without a local MOVE.
        for variable, scopes in comparison_scopes.items():
            if variable not in self.station_scopes and len(scopes) == 1:
                self.station_scopes[variable].update(scopes)

        for (variable, value, _), rows in observations.items():
            notes = rows[:1]
            if self.source_format == 'smc2':
                comparisons = [r for r in rows if r['kind'] in ('comparison', 'st_comparison')
                               and r['operator'] == '=']
                notes = comparisons or [r for r in rows if r['kind'] in ('assignment', 'st_assignment')]
            for row in notes:
                self._note((variable, value), row['comment'], row['entry'], str(row['network']),
                           row['node'], 'PLC XML', 1)

    def _note(self, state, comment, scope, network, source_id, source_file, rank):
        text = _comment_text(comment, state[1])
        if not text:
            return
        item = {'text': text, 'raw': comment, 'scope': scope, 'network': network,
                'source_id': source_id, 'source_file': source_file,
                'priority': rank + (3 if _STEP_PREFIX.match(comment) else 0)}
        item['id'] = stable_id('TC', item)
        if item not in self.notes[state]:
            self.notes[state].append(item)

    def _add_edge(self, variable, edge):
        self.transitions[variable, edge['from'], edge['to']].append(edge)
        self.graphs[variable][edge['scope'], edge['binding']][edge['from']].append(edge)
        self.bindings[variable].add(edge['binding'])

    def _control_description(self, start, relation, direct, commands):
        notes = self.notes.get(start, [])
        if relation == 'ambiguous_scope':
            return '', [], [], notes, ['ambiguous_scope']
        direct_ids = {e['source_id'] for e in direct}
        locations = {(e['scope'], e['network']) for e in direct}
        applicable = [note for note in notes if note['applies_to_step']]
        if direct:
            # Directly bound notes are specific to this branch. A note on
            # another outgoing branch remains visible only as context.
            applicable = [note for note in applicable if note['source_id'] in direct_ids]
        descriptions = {n['text'] for n in applicable}
        linked = [c for c in commands if (not locations or (c['scope'], c['network']) in locations)
                  and self.control.compatible_write(start, c, direct)]
        reasons = set(self.control.affected.get(start[0], ())) if relation == 'unresolved' else set()
        if start in self.control.partial:
            reasons.add('unresolved_guard')
        if any(write['to'] is None for write in self.control.state_writes.get(start, ())):
            reasons.add('dynamic_state_write')
        if len(descriptions) == 1:
            return applicable[0]['text'], [n['id'] for n in applicable], linked, notes, sorted(reasons)
        if len(descriptions) > 1:
            reasons.add('conflicting_comments')
        if not notes and not linked and not self.control.state_writes.get(start):
            reasons.add('no_local_control')
        applicable_ids = {note['id'] for note in applicable}
        context_notes = [{**note, 'applies_to_step': note['id'] in applicable_ids} for note in notes]
        return '', [], linked, context_notes, sorted(reasons)

    def lookup(self, start_label, end_label, step_comment=''):
        start, end = _state(start_label), _state(end_label)
        direct, path, commands, linked, refs, notes = [], [], [], [], [], []
        relation, description, description_source = 'unresolved', '', ''
        issues, writes, blockers = [], [], []
        if start and end and start[0] == end[0]:
            variable, value = start
            direct = self.transitions.get((variable, value, end[1]), [])
            proved = [e for e in direct if e['guard_complete']]
            scopes = {e['scope'] for e in direct}
            if self.control is not None:
                writes = self.control.state_writes.get(start, [])
            local_programs = {write['program'] for write in writes}
            if len(self.bindings[variable]) > 1 or len(scopes) > 1 or len(local_programs) > 1:
                relation = 'ambiguous_scope'
            elif proved:
                relation = 'direct'
            elif direct:
                relation = 'direct_unverified'
            else:
                for allow_zero, proved_only in ((False, True), (False, False), (True, True), (True, False)):
                    paths = [
                        _path(graph, value, end[1], proved_only=proved_only, allow_zero=allow_zero)
                        for graph in self.graphs[variable].values()
                    ]
                    path = min((p for p in paths if len(p) > 1), key=len, default=[])
                    if path:
                        relation = 'path_via_zero_candidate' if allow_zero else 'path_candidate'
                        break

            commands = self.commands.get(start, [])
            if self.control is not None:
                step_comment = ''
                description, refs, linked, notes, issues = self._control_description(start, relation, direct, commands)
                if description:
                    description_source = 'A档(源码控制注释)'
                if 'no_local_control' in issues:
                    blockers = self.control.blockers.get(variable, [])
            elif relation == 'direct':
                locations = {(e['scope'], e['network']) for e in proved}
                linked = [
                    c for c in commands
                    if c['kind'] and c['guard_complete'] and c['arguments'] and c['network']
                    and len(locations) == 1 and (c['scope'], c['network']) in locations
                ]
                comments = [_comment_text(e['comment'], value) for e in proved]
                if all(comments) and len(set(comments)) == 1:
                    description, description_source = comments[0], 'A档(源码网络注释)'
                    refs = [e['source_id'] for e in proved]
                elif not any(e['comment'].strip() for e in proved):
                    description = _comment_text(step_comment, value)
                    if description:
                        description_source = 'A档(源码步注释)'
                        refs = [f'step_comments.json:{start_label}']
            # A description identifies the start state; proving a direct edge
            # is a separate question and must not suppress an available note.
            if self.control is None:
                exact_note = _comment_text(step_comment, value)
                notes = self.notes.get(start, [])
                if exact_note:
                    description, description_source = exact_note, 'A档(源码步注释)'
                    refs = [f'step_comments.json:{start_label}']
                elif not description and notes:
                    scoped = [n for n in notes if not scopes or n['scope'] in scopes] or notes
                    priority = max(n['priority'] for n in scoped)
                    best = [n for n in scoped if n['priority'] == priority]
                    if len({n['text'] for n in best}) == 1:
                        description, description_source = best[0]['text'], 'A档(起始步源码注释)'
                        refs = list(dict.fromkeys(n['id'] for n in best))
            context = (self.context.packet(start, end, relation, direct, path,
                                           linked if self.control is not None else commands, notes, issues,
                                           writes=writes, blockers=blockers)
                       if not description else {})
        else:
            context = {}
        return {
            'relation': relation, 'direct': direct, 'candidate_path': path,
            'start_comment': step_comment, 'start_commands': commands, 'linked_commands': linked,
            'start_notes': notes,
            'description': description, 'description_source': description_source,
            'description_refs': refs,
            'source_issues': issues,
            'start_state_writes': writes, 'source_blockers': blockers,
            'description_context': context,
            'basis': context.get('basis', ''),
        }


def read_trace_evidence(run):
    """Read the row-level source evidence of a Trace run."""
    run = Path(run)
    if not (run / 'trace_collection_pairs.csv').is_file():
        raise ValueError('缺少 trace_collection_pairs.csv')
    path = run / EVIDENCE_FILE
    if not path.is_file():
        return {}
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)['pairs']
