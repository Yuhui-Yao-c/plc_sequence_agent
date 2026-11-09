"""Trace description evidence and checks independent of process vocabulary."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from atomic_boundaries import stable_id
from boundary_logic import IDENT, symbol as normalize_symbol
from trace_call_evidence import action_details, action_specs, named_arguments
from trace_source_evidence import _comment_text

MEMORY_FILE = 'trace_semantic_memory.json'
SCHEMA = 'trace-description-context-v4'
LEGACY_CLAUSE_SCHEMA = 'trace-description-context-v3'
_SYMBOL = re.compile(IDENT)
_NUMBER = r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?'
_EXPRESSION_WORDS = {'and', 'or', 'xor', 'not', 'true', 'false', 'mod'}


def _read(path):
    if path.is_file():
        with path.open(encoding='utf-8-sig', newline='') as stream:
            yield from csv.DictReader(stream)


def _json(path, default):
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8-sig'))
    return default


def _clean(text):
    text = str(text or '')
    match = re.search(r'<GrpComment_zh-cn>(.*?)</GrpComment_zh-cn>', text, re.S)
    if match:
        text = match[1]
    text = re.split(r'<Sub\s*/?>', text)[0]
    return re.sub(r'<[^>]+>', '', text).strip()


def _family(symbol):
    """Keep the namespace intact; generalize digits in the last member only."""
    parent, dot, member = symbol.rpartition('.')
    changed = re.sub(r'\d+', '#', member)
    return (parent + dot + changed).casefold() if changed != member else ''


def _symbols(text):
    out = set()
    text = re.sub(r'\b(?:T|TIME|LTIME)#[\w.]+', '', text or '', flags=re.I)
    for match in _SYMBOL.finditer(text or ''):
        parts = normalize_symbol(match[0]).split('.')
        out.update('.'.join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


def _compact_command(command):
    fields = ('id', 'scope', 'network', 'source_line', 'node', 'command', 'arguments',
              'guard', 'guard_complete', 'comment', 'issue', 'role', 'kind', 'operation')
    return {key: command.get(key, '') for key in fields}


class DescriptionContext:
    def __init__(self, run, index, wanted):
        self.run, self.index = Path(run), index
        docs = _json(self.run / 'variable_documentation.json', {})
        self.docs = {key.casefold(): (key, _clean(value))
                     for key, value in (docs.get('all') or {}).items() if _clean(value)}
        self.families = defaultdict(list)
        entries = []
        for symbol, text in self.docs.values():
            family = _family(symbol)
            if not family:
                continue
            item = {'symbol': symbol, 'text': text, 'source': 'variable_documentation.json'}
            item['id'] = stable_id('TM', item)
            entries.append(item)
            self.families[family].append(item)
        # Find family examples by parameter binding or an observed source
        # write. Keep the annotation as context, without extracting a noun class.
        for commands in index.commands.values():
            for command in commands:
                if not command.get('comment'):
                    continue
                actuals = named_arguments(command['arguments'])
                effects = list(index.call_evidence.effects(command, actuals))
                objects = {effect['actual'] for effect in effects}
                objects.update(spec[0] for spec in action_specs(command, actuals, effects))
                for symbol in sorted(objects):
                    family = _family(symbol)
                    if not family:
                        continue
                    item = {'symbol': symbol, 'text': command['comment'],
                            'source': 'annotated_call', 'source_ref': command['id'], 'scope': command['scope']}
                    item['id'] = stable_id('TM', item)
                    entries.append(item)
                    self.families[family].append(item)
        self.calls = defaultdict(list)
        self.related = defaultdict(list)
        used_symbols = set()
        for commands in index.commands.values():
            for command in commands:
                used_symbols.update(_symbols(command.get('arguments', '')))
        used_symbols = {s.casefold() for s in used_symbols
                        if ('.' in s or '[' in s or len(s) >= 6) and s.casefold() not in wanted}
        pattern = re.compile(r'(?<![\w.\[\]])(' + '|'.join(
            re.escape(name) for name in sorted(wanted, key=len, reverse=True)) + r')\s*=\s*([+-]?\d+)', re.I)
        for event in _read(self.run / 'indexed_events.csv'):
            guard = event.get('guard', '')
            starts = {(m[1].casefold(), int(m[2])) for m in pattern.finditer(guard)}
            item = {key: event.get(key, '') for key in
                    ('source_unit', 'network', 'guard', 'command', 'arguments', 'feedback', 'next_state', 'comment', 'relation')}
            item['id'] = stable_id('TF', item)
            for state in starts:
                self.calls[state].append(item)
            # One source-backed hop through an actual bound/shared symbol.
            # Common enables and bare station roots do not create a call graph.
            event_symbols = {normalize_symbol(m[0]).casefold() for m in _SYMBOL.finditer(' '.join(
                item.get(key, '') for key in ('arguments', 'guard', 'feedback', 'next_state')))}
            for symbol in event_symbols & used_symbols:
                if len(self.related[symbol]) < 24:
                    self.related[symbol].append(item)
        annotated = []
        for state, commands in index.commands.items():
            for command in commands:
                if command.get('comment'):
                    annotated.append({'state': [state[0], state[1]], **_compact_command(command)})
        # Hash source evidence, not run paths, timestamps or previous LLM results.
        digest = hashlib.sha256()
        for name in ('variable_documentation.json', 'native_command_evidence.csv',
                     'source_stage_candidates.csv', 'indexed_candidates.csv', 'indexed_events.csv', 'step_comments.json'):
            path = self.run / name
            digest.update(name.encode())
            if path.is_file():
                digest.update(path.read_bytes())
        self.fingerprint = digest.hexdigest()
        memory = {'schema': 2, 'source_fingerprint': self.fingerprint,
                  'policy': '同工程源码命名及注释示例。名称类比仅作对象识别提示。动作、目标及完成状态须有独立本步证据。',
                  'naming_examples': entries, 'annotated_commands': annotated}
        (self.run / MEMORY_FILE).write_text(json.dumps(memory, ensure_ascii=False, indent=1), encoding='utf-8')

    def packet(self, start, end, relation, direct, path, commands, notes):
        variable, number = start
        evidence, seen = [], set()

        def add(kind, data, *, local=False, **extra):
            item = {'category': kind, 'local': local, 'data': data, **extra}
            item['id'] = stable_id('TX', item)
            truncated = [key for key, value in data.items() if isinstance(value, str) and len(value) > 1600]
            if truncated:
                item['data'] = {key: value[:1600] + '…' if key in truncated else value
                                for key, value in data.items()}
                item['truncated_fields'] = truncated
                item['full_source'] = 'trace_source_evidence.json中的本行start_commands或原始源码证据CSV'
            if item['id'] not in seen:
                seen.add(item['id']); evidence.append(item)
            return item['id']

        add('observed_pair', {'variable': variable, 'from': number, 'to': end[1]}, local=True)
        command_refs, action_keys, required = {}, set(), []
        for command in commands:
            c = _compact_command(command)
            actuals = named_arguments(c['arguments'])
            effects = list(self.index.call_evidence.effects(c, actuals)) if actuals else []
            specs = list(action_specs(c, actuals, effects)) if relation != 'ambiguous_scope' else []
            if len(command_refs) >= 24 and not specs:
                continue
            prefix = re.match(r'^\s*(?:步\s*)?(\d+)\s*[-—:：>]', c['comment'])
            usable_comment = not prefix or int(prefix[1]) == number
            ref = add('current_command', c, local=True, comment_matches_step=usable_comment)
            command_refs[command['id']] = ref
            effect_refs = {}
            for effect in effects:
                effect_refs[effect['target'], effect['callee_node']] = add(
                    'bound_write', {'call_ref': ref, **effect}, local=True,
                    caveat='实参代入后的源码条件写入，保留门控，不能证明现场输出或到位')
            for actual, mechanism, operation, target, writes in specs:
                binding = self.index.call_evidence.binding(c['scope'], actual)
                doc = self.docs.get(actual.casefold(), ('', ''))[1]
                label, operation, phrase = action_details(actual, mechanism, operation, target, doc, binding)
                if not label:
                    continue
                key = (c['scope'], actual.casefold(), operation, target)
                if key in action_keys:
                    continue
                action_keys.add(key)
                label_ref = add('object_binding', binding) if binding else ''
                required.append(add('action_anchor', {
                    'call_ref': ref, 'actual': actual, 'object': label, 'operation': operation,
                    'target': target, 'phrase': phrase, 'object_documentation': doc or (binding or {}).get('text', ''),
                    'label_ref': label_ref,
                    'write_refs': [effect_refs[e['target'], e['callee_node']] for e in writes],
                }, local=True))
        # A timer that drives an alarm coil is evidence of monitoring. Its
        # preset must not become the observed dwell or a normal branch delay.
        included = [c for c in commands if c['id'] in command_refs]
        for timer in included:
            if timer['role'] != 'timer' or not timer['node']:
                continue
            for coil in included:
                if (coil['command'] == 'coil_set' and coil['scope'] == timer['scope']
                        and coil['network'] == timer['network']
                        and re.search(r'alarm|timeout', coil['arguments'], re.I)
                        and (';' + timer['node'] + ';') in (';' + coil['path'] + ';')):
                    add('timer_alarm', {'timer_ref': command_refs[timer['id']], 'alarm_ref': command_refs[coil['id']],
                                        'target': coil['arguments'], 'operation': '超时监控'}, local=True)
        for note in notes[:12]:
            add('start_comment', note, local=True)
        for edge in direct[:12]:
            add('direct_control', edge, local=True)
        for edge in path[:16]:
            add('candidate_path', edge, local=edge['from'] == number,
                caveat='静态候选路径，不证明本次采样间实际逐条执行')
        neighbors = []
        for (scope, binding), graph in self.index.graphs.get(variable, {}).items():
            neighbors.extend(graph.get(number, []))
            neighbors.extend(edge for edges in graph.values() for edge in edges if edge['to'] == number)
        for edge in neighbors[:16]:
            add('control_context', edge, local=edge['from'] == number)
            neighbor = edge['to'] if edge['from'] == number else edge['from']
            for note in self.index.notes.get((variable, neighbor), [])[:2]:
                add('neighbor_comment', note, caveat='相邻源码状态的注释，不能当成本步注释')
        for call in self.calls.get(start, [])[:12]:
            add('guarded_call', call, local=True,
                caveat='保留原始门控和调用实参，门控中的否定和分支必须由LLM检查')

        local_scopes = {c.get('scope', '') for c in commands} | {e['scope'] for e in direct + path[:1]}
        related = {}
        for command in commands:
            for match in _SYMBOL.finditer(command.get('arguments', '')):
                actual = normalize_symbol(match[0])
                for call in self.related.get(actual.casefold(), []):
                    if '.' not in actual and call['source_unit'] not in local_scopes:
                        continue
                    if call not in self.calls.get(start, []):
                        related.setdefault(call['id'], (actual, call))
        for actual, call in list(related.values())[:8]:
            add('related_call', {'shared_symbol': actual, **call},
                caveat='调用实参或门控共用同一符号的一跳关联，需LLM结合作用域判断，不能直接当成本步动作')

        symbols = set()
        for item in evidence:
            data = item['data']
            for key in ('arguments', 'guard', 'feedback', 'next_state', 'target'):
                symbols.update(_symbols(data.get(key, '')))
        local_symbols = set()
        for command in commands:
            local_symbols.update(_symbols(command.get('arguments', '')))
            local_symbols.update(_symbols(command.get('guard', '')))
        def rank(symbol):
            documented = (symbol.casefold() in self.docs
                          or re.sub(r'\[\d+\]', '', symbol).casefold() in self.docs)
            return (not documented, symbol)
        ordered = sorted(local_symbols, key=rank) + sorted(symbols - local_symbols, key=rank)
        for symbol in ordered:
            found = self.docs.get(symbol.casefold()) or self.docs.get(re.sub(r'\[\d+\]', '', symbol).casefold())
            if found:
                name, text = found
                add('exact_mapping', {'symbol': symbol, 'declared_symbol': name, 'text': text,
                                      'source': 'variable_documentation.json'},
                    caveat='数组声明文档适用于类型，不证明某个数组项的独有方向或用途' if symbol.casefold() != name.casefold() else '')
            if (not found or not re.search(r'[\u4e00-\u9fff]', found[1])) and (family := _family(symbol)):
                examples = self.families.get(family, [])
                examples = [item for item in examples if item.get('source') != 'annotated_call'
                            or item.get('scope') in local_scopes]
                same_object = [item for item in examples if item['symbol'].casefold() == symbol.casefold()]
                for example in (same_object or examples)[:2]:
                    add('annotated_mapping' if same_object else 'naming_analogy',
                        {'target': symbol, 'example_symbol': example['symbol'],
                         'example_text': example['text'], 'source_ref': example['id']},
                        caveat='名称提示。动作、方向、目标及反馈须引用本步证据。')
            if len(evidence) >= 100:
                break
        packet = {'schema': SCHEMA, 'relation': relation,
                  'memory_file': MEMORY_FILE,
                  'source_fingerprint': self.fingerprint, 'evidence': evidence,
                  'required_actions': required,
                  'limitations': ['描述起始步；结束步注释和静态路径只作上下文',
                                 '调用实参、门控和映射可共同支持推断；机构同类命名只支持类别推断',
                                 '不得仅按步号大小、0值、工位名或相邻展示行猜动作']}
        packet['basis'] = stable_id('TB4', [start, end, packet])
        return packet


def _clause_refs(refs, catalog):
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in catalog for ref in refs):
        return None
    return [catalog[ref] for ref in dict.fromkeys(refs)]


def _target_supported(target, parameter, arguments, comment):
    if not parameter:
        return target in comment
    value = named_arguments(arguments).get(parameter.casefold(), '')
    if not value:
        return False
    literal = re.fullmatch(rf'(?:[A-Za-z]+#)?({_NUMBER})', value)
    if literal:
        try:
            return {Decimal(n) for n in re.findall(_NUMBER, target)} == {Decimal(literal[1])}
        except InvalidOperation:
            return False
    value = value.strip("'\"")
    return bool(value and value in target)


def _ground_clause(clause, subject, action, target, catalog, reserved, observed):
    if not subject or not action or subject == action:
        return '分句须分别填写具体对象和动作', []
    ref = clause.get('source_ref')
    fact = catalog.get(ref) if isinstance(ref, str) else None
    if not fact or not fact['local']:
        return '分句须引用本步指令效果、控制条件或适用注释作为source_ref', []
    if ref in reserved:
        return '该指令已由动作锚点覆盖，补充分句须引用另一项本步证据', []

    category, data = fact['category'], fact['data']
    comment = _comment_text(data.get('comment', ''), observed['from'])
    if category == 'start_comment':
        comment = data['text']
    symbols = set()
    arguments = ''
    parent = None
    if category in ('bound_write', 'timer_alarm'):
        symbols.update((data.get('actual', ''), data['target']))
        parent = catalog[data['call_ref']] if category == 'bound_write' else None
    elif category == 'direct_control' and data.get('guard_complete'):
        symbols.update(normalize_symbol(m[0]) for m in _SYMBOL.finditer(data['guard']))
    elif category == 'current_command' and (comment or data.get('role') in {
            'actuator_command', 'signal_write', 'connected_signal'}):
        arguments = data['arguments']
        for actual in named_arguments(arguments).values() or [arguments]:
            symbols.update(normalize_symbol(m[0]) for m in _SYMBOL.finditer(actual))
    elif category != 'start_comment':
        return '此引用仅可作上下文，缺少本步动作效果或条件依据', []
    if parent:
        arguments = parent['data']['arguments']
    bound = {s.casefold() for s in symbols} - _EXPRESSION_WORDS - {observed['variable'].casefold(), ''}
    actual = clause.get('subject_symbol', '')
    if not isinstance(actual, str):
        return 'subject_symbol须为引用证据中的完整PLC对象或条件符号', []
    if actual.strip():
        if normalize_symbol(actual).casefold() not in bound:
            return 'subject_symbol与所引用的本步对象或条件不匹配', []
    elif not comment or subject not in comment or action not in comment:
        return '分句须绑定本步对象或条件符号，或引用包含该对象和动作的本步注释', []
    parameter = clause.get('target_parameter', '')
    if not isinstance(parameter, str) or (target and not _target_supported(target, parameter.strip(), arguments, comment)):
        return f'目标“{target}”缺少本步注释或调用实参支持', []
    return '', [ref] + ([parent['id']] if parent else [])


def _validate_action_clauses(result, context):
    catalog = {item['id']: item for item in context['evidence']}
    required = set(context['required_actions'])
    actions = result.get('actions')
    if not isinstance(actions, list) or not actions:
        return None, 'B档须提交动作分句actions，逐项交代对象和动作'
    reasoning, kind = result.get('reasoning'), result.get('kind')
    if not isinstance(kind, str) or not kind.strip() or not isinstance(reasoning, str) or not reasoning.strip():
        return None, '须回传行性质和基于引用的推断说明reasoning'
    legacy = context.get('schema') == LEGACY_CLAUSE_SCHEMA
    if legacy:
        from trace_description_legacy import _KINDS, check_v3_phrase
        if kind not in _KINDS:
            return None, '须回传行性质和基于引用的推断说明reasoning'

    observed = next(item['data'] for item in catalog.values() if item['category'] == 'observed_pair')
    # A free clause may describe another source effect, but may not rewrite
    # the instruction already covered by a deterministic anchor.
    anchored = {(catalog[ref]['data']['call_ref'], catalog[ref]['data']['actual'].casefold())
                for ref in required}
    reserved = {call for call, _ in anchored}
    reserved.update(item['id'] for item in catalog.values() if item['category'] == 'bound_write'
                    and (item['data']['call_ref'], item['data']['actual'].casefold()) in anchored)
    used, refs, phrases = set(), [], []
    for clause in actions:
        if not isinstance(clause, dict):
            return None, '动作分句须为JSON对象'
        selected = _clause_refs(clause.get('evidence_refs', []), catalog)
        if selected is None:
            return None, '动作分句须引用本行证据包中的有效证据ID'
        anchor = clause.get('action_ref')
        if anchor is not None:
            if not isinstance(anchor, str) or anchor not in required or anchor in used:
                return None, 'action_ref须为本行未重复的动作锚点'
            used.add(anchor)
            data = catalog[anchor]['data']
            phrases.append(data['phrase'])
            refs.extend([anchor, data['call_ref'], *data['write_refs']])
            if data['label_ref']:
                refs.append(data['label_ref'])
        else:
            values = [clause.get(key, '') for key in ('subject', 'action', 'target')]
            if any(not isinstance(v, str) or len(v) > 80 or '\n' in v or '\r' in v for v in values):
                return None, '对象、动作、目标字段须为单行短语'
            subject, action, target = (v.strip() for v in values)
            if legacy:
                issue = check_v3_phrase(subject, action, target, selected, observed['from'], _symbols)
            else:
                issue, grounding = _ground_clause(clause, subject, action, target, catalog, reserved, observed)
                refs.extend(grounding)
            if issue:
                return None, issue
            phrases.append(subject + action + target)
        refs.extend(item['id'] for item in selected)
    if missing := required - used:
        labels = [catalog[ref]['data']['object'] for ref in context['required_actions'] if ref in missing]
        return None, '遗漏本步已绑定的动作对象 ' + '、'.join(dict.fromkeys(labels))

    # The same actuator may have alternative commands under different guards.
    # Keep those phrases conditional instead of implying a sequence of moves.
    objects = [catalog[ref]['data']['actual'].casefold() for ref in used]
    prefix = '按分支条件执行，' if len(objects) != len(set(objects)) else ''
    level = ('同类命名推断' if any(catalog[ref]['category'] == 'naming_analogy' for ref in refs)
             else '上下文证据推断')
    return {'desc': prefix + '，'.join(phrases), 'kind': result['kind'],
            'refs': list(dict.fromkeys(refs)), 'reasoning': reasoning.strip(),
            'evidence_level': level}, ''


def validate_context_result(result, context):
    """Check provenance/format and unsupported specifics; semantics stay inferred."""
    if not isinstance(result, dict) or result.get('basis') != context.get('basis'):
        return None, 'B档证据版本不匹配'
    if result.get('ok') is not True:
        return None, str(result.get('reason') or 'B档证据不足')
    if context.get('schema') in {SCHEMA, LEGACY_CLAUSE_SCHEMA}:
        return _validate_action_clauses(result, context)
    from trace_description_legacy import validate_v2_result
    return validate_v2_result(result, context)
