"""Link an observed trace pair to local PLC evidence without altering the jump."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict, deque
from pathlib import Path

from atomic_boundaries import PROVEN, atomic, stable_id

EVIDENCE_FILE = 'trace_source_evidence.json'
RELATION_TEXT = {
    'direct': '源码直跳有证据',
    'direct_unverified': '源码直跳门控待核',
    'path_candidate': '源码多步路径待核',
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
_TEMPLATE = re.compile(r'^相邻已观察|^源码局部状态迁移|^步进保持|^迁移（')


def _read_csv(path):
    if path.is_file():
        with path.open(encoding='utf-8-sig', newline='') as stream:
            yield from csv.DictReader(stream)


def _state(label):
    value = atomic(label)
    if value is not None and value.kind == 'eq':
        variable, number = value.args
        return variable.casefold(), number
    return None


def _comment_text(comment, source_step):
    lines = comment.strip().splitlines()
    if not lines or _TEMPLATE.search(lines[0]):
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


def _path(adjacency, source, target, *, proved_only):
    pending = deque([source])
    previous = {source: None}
    while pending and target not in previous:
        current = pending.popleft()
        for edge in adjacency.get(current, ()):
            if proved_only and not edge['guard_complete']:
                continue
            dest = edge['to']
            if dest == 0 and dest != target:
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
    def __init__(self, run, variables):
        self.transitions = defaultdict(list)
        self.graphs = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        self.bindings = defaultdict(set)
        self.commands = defaultdict(list)
        wanted = {name.casefold() for name in variables}
        run = Path(run)
        edges_by_id = {}
        for row in _read_csv(run / 'source_transition_edges.csv'):
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

        for row in _read_csv(run / 'source_stage_candidates.csv'):
            start = _state(row.get('开始标签'))
            end = _state(row.get('结束标签'))
            scope = row.get('证据POU', '')
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

        for row in _read_csv(run / 'native_command_evidence.csv'):
            start = _state(row['start'])
            if start and start[0] in wanted:
                role, kind, operation = _command_role(row['command'])
                record = {
                    **row, 'role': role, 'kind': kind, 'operation': operation,
                    'guard_complete': row['guard_complete'] == 'True',
                }
                record['id'] = stable_id('TE', record)
                self.commands[start].append(record)

        for row in _read_csv(run / 'plcopen_action_trace.csv'):
            start = _state(f"{row['state_variable']}={row['state_value']}")
            if start and start[0] in wanted and row['target_type'] == 'coil' and row['target_symbol']:
                record = {
                    'scope': row['source_unit'], 'network': '', 'node': row['target_id'],
                    'source_line': '', 'command': 'coil_connected', 'arguments': row['target_symbol'],
                    'guard': row['boundary_condition'], 'guard_complete': False,
                    'role': 'connected_signal', 'kind': '', 'operation': '', 'comment': '',
                    'issue': row['caveat'], 'path': row['path_ids'], 'start': '',
                }
                record['id'] = stable_id('TE', record)
                self.commands[start].append(record)

    def _add_edge(self, variable, edge):
        self.transitions[variable, edge['from'], edge['to']].append(edge)
        self.graphs[variable][edge['scope'], edge['binding']][edge['from']].append(edge)
        self.bindings[variable].add(edge['binding'])

    def lookup(self, start_label, end_label, step_comment):
        start, end = _state(start_label), _state(end_label)
        direct, path, commands, linked, eligible, refs = [], [], [], [], [], []
        relation, description, description_source = 'unresolved', '', ''
        if start and end and start[0] == end[0]:
            variable, value = start
            direct = self.transitions.get((variable, value, end[1]), [])
            proved = [e for e in direct if e['guard_complete']]
            scopes = {e['scope'] for e in direct}
            if len(self.bindings[variable]) > 1 or len(scopes) > 1:
                relation = 'ambiguous_scope'
            elif proved:
                relation = 'direct'
            elif direct:
                relation = 'direct_unverified'
            else:
                for proved_only in (True, False):
                    paths = [
                        _path(graph, value, end[1], proved_only=proved_only)
                        for graph in self.graphs[variable].values()
                    ]
                    path = min((p for p in paths if len(p) > 1), key=len, default=[])
                    if path:
                        relation = 'path_candidate'
                        break

            commands = self.commands.get(start, [])
            if relation == 'direct':
                locations = {(e['scope'], e['network']) for e in proved}
                linked = [
                    c for c in commands
                    if c['kind'] and c['guard_complete'] and c['arguments'] and c['network']
                    and len(locations) == 1 and (c['scope'], c['network']) in locations
                ]
                main_commands = [c for c in linked if c['role'] == 'actuator_command'] or linked
                # A single description needs a single distinguishable principal instruction.
                targets = {(c['command'], c['arguments']) for c in main_commands}
                if len(targets) == 1:
                    eligible = [
                        {**c, 'description_comment': _comment_text(c['comment'], value)}
                        for c in main_commands
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
        return {
            'relation': relation, 'direct': direct, 'candidate_path': path,
            'start_comment': step_comment, 'start_commands': commands, 'linked_commands': linked,
            'description': description, 'description_source': description_source,
            'description_refs': refs, 'b_evidence': eligible,
            'basis': stable_id('TB', [start_label, end_label, direct, eligible]) if eligible else '',
        }


def read_trace_evidence(run):
    """Return None for prediction runs and an empty map for legacy Trace runs."""
    run = Path(run)
    if not (run / 'trace_collection_pairs.csv').is_file():
        return None
    path = run / EVIDENCE_FILE
    if not path.is_file():
        return {}
    with path.open(encoding='utf-8') as stream:
        return json.load(stream)['pairs']
