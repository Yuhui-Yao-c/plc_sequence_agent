"""Bounded, source-backed context for Trace description batches.

Memory is rebuilt from this project's documentation, annotated commands and
wordbook. Naming analogies transfer a mechanism class, never a direction or
target position. LLM candidates are not promoted back into source facts.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from atomic_boundaries import stable_id
from trace_call_evidence import action_specs, device_label, named_arguments
from trace_source_evidence import _comment_text

MEMORY_FILE = 'trace_semantic_memory.json'
SCHEMA = 'trace-description-context-v3'
_SYMBOL = re.compile(r'[A-Za-z_][\w]*(?:\[\d+\])?(?:\.[A-Za-z_]\w*(?:\[\d+\])?)*')
_CLASSES = ('气缸', '伺服', '电机', '机器人', '吸盘', '真空', '定时器')
_KINDS = {'机械动作', '加工保持', '检测测量', '工艺等待', '交接数据', '周期汇总', '内部辅助', '信号/标志', '步进', '未分类'}
_SPECIFIC = re.compile(
    r'破真空|吸附|吹气|停止吸气|松开|夹紧|夹取|张开|闭合|伸出|缩回|上升|下降|'
    r'正转|反转|回原位|回零|焊接|点胶|贴胶|到位|完成|超时监控|定时等待|延时'
)
_VERB = re.compile(
    r'判断|选择|检测|测量|检查|监控|等待|定位|运行|搬运|移载|移动|保持|计时|'
    r'请求|发出|置位|复位|写入|清零|保存|读取|记录|发送|接收|传递|允许|确认|'
    r'启动|停止|切换|报警|屏蔽|触发|进入|退出|取料|放料|取件|放件|'
    r'扫码|拍照|前进|后退|旋转|伸出|缩回|回原位|回零|夹紧|松开|闭合|张开|'
    r'吸附|破真空|吹气|上升|下降|正转|反转|焊接|点胶|贴胶|剪切|贴合|握手'
)
_GENERIC_SUBJECTS = {'本步', '步骤', '设备', '机构', '系统', '工艺', '动作', '信号', '流程', '状态'}


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
        parts = match[0].split('.')
        out.update('.'.join(parts[:i]) for i in range(1, len(parts) + 1))
    return out


def _compact_command(command):
    fields = ('id', 'scope', 'network', 'source_line', 'node', 'command', 'arguments',
              'guard', 'guard_complete', 'comment', 'issue', 'role', 'kind', 'operation')
    return {key: command.get(key, '') for key in fields}


def _object_label(docs, binding, actual, mechanism):
    doc = docs.get(actual.casefold())
    if doc and re.search(r'[\u4e00-\u9fff]', doc[1]):
        return device_label(doc[1]), doc[1]
    if binding:
        return binding['label'], binding['text']
    indexed = re.fullmatch(r'(Cylinder|Vacuum)\[(\d+)\]', actual, re.I)
    if indexed:
        mechanism = {'cylinder': '气缸', 'vacuum': '真空通道'}[indexed[1].casefold()]
        return f'{indexed[2]}号{mechanism}', ''
    # An axis with no Chinese documentation can still retain its channel index.
    number = re.search(r'\d+', actual.rsplit('.', 1)[-1])
    return (f'{int(number[0])}号{mechanism}' if number and mechanism else mechanism), ''


class DescriptionContext:
    def __init__(self, run, index, wanted):
        self.run, self.index = Path(run), index
        docs = _json(self.run / 'variable_documentation.json', {})
        self.docs = {key.casefold(): (key, _clean(value))
                     for key, value in (docs.get('all') or {}).items() if _clean(value)}
        self.wordbook = _json(Path(__file__).with_name('action_wordbook.json'), {})
        self.families = defaultdict(list)
        entries = []
        for symbol, text in self.docs.values():
            classes = [word for word in _CLASSES if word in text]
            if not classes:
                continue
            item = {'symbol': symbol, 'text': text, 'mechanism_classes': classes,
                    'source': 'variable_documentation.json'}
            item['id'] = stable_id('TM', item)
            entries.append(item)
            if family := _family(symbol):
                self.families[family].append(item)
        # Learn object classes from annotated, explicitly bound actuator calls.
        # Only their object parameter is learned; alarm/enable arguments are not
        # cylinders just because they occur in the same invocation.
        for commands in index.commands.values():
            for command in commands:
                classes = [word for word in _CLASSES if word in command.get('comment', '')]
                kind = command.get('command', '').casefold()
                if not classes:
                    continue
                if kind in ('axismove', 'mc_moveabsolute', 'mc_moverelative', 'mc_movevelocity'):
                    parameter = r'_?Axis'
                elif kind in ('onecycontrol', 'twocycontrol', 'fourcycontrol'):
                    parameter = r'CY(?:_[1-4])?'
                else:
                    continue
                for match in re.finditer(r'(?:^|;)\s*(' + parameter + r')\s*=\s*([^;]+)', command.get('arguments', ''), re.I):
                    symbol = match[2].strip()
                    if not _SYMBOL.fullmatch(symbol):
                        continue
                    item = {'symbol': symbol, 'text': command['comment'], 'mechanism_classes': classes,
                            'source': 'annotated_call', 'source_ref': command['id'], 'scope': command['scope']}
                    item['id'] = stable_id('TM', item)
                    entries.append(item)
                    if family := _family(symbol):
                        self.families[family].append(item)
        self.calls = defaultdict(list)
        self.related = defaultdict(list)
        used_symbols = set()
        for commands in index.commands.values():
            for command in commands:
                used_symbols.update(m[0] for m in _SYMBOL.finditer(command.get('arguments', '')))
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
            event_symbols = {m[0].casefold() for m in _SYMBOL.finditer(' '.join(
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
        digest.update(json.dumps(self.wordbook, ensure_ascii=False, sort_keys=True).encode())
        self.fingerprint = digest.hexdigest()
        memory = {'schema': 1, 'source_fingerprint': self.fingerprint,
                  'policy': '同工程源码映射及注释示例；命名类比仅转移机构类别，不能转移方向、位置或完成状态；LLM结果不作为源码事实回流',
                  'mechanism_mappings': entries, 'annotated_commands': annotated,
                  'wordbook': self.wordbook}
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
                label, doc = _object_label(self.docs, binding, actual, mechanism)
                if not label:
                    continue
                key = (c['scope'], actual.casefold(), operation, target)
                if key in action_keys:
                    continue
                action_keys.add(key)
                label_ref = add('object_binding', binding) if binding else ''
                if operation == '原位侧请求' and label.endswith('气缸'):
                    operation = '回原位请求'
                phrase = f'向{label}发出{operation}' if operation.endswith('请求') else f'{label}{operation}{target}'
                required.append(add('action_anchor', {
                    'call_ref': ref, 'actual': actual, 'object': label, 'operation': operation,
                    'target': target, 'phrase': phrase, 'object_documentation': doc, 'label_ref': label_ref,
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
                symbol = match[0]
                for call in self.related.get(symbol.casefold(), []):
                    if '.' not in symbol and call['source_unit'] not in local_scopes:
                        continue
                    if call not in self.calls.get(start, []):
                        related.setdefault(call['id'], (symbol, call))
        for symbol, call in list(related.values())[:8]:
            add('related_call', {'shared_symbol': symbol, **call},
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
            found = self.docs.get(re.sub(r'\[\d+\]', '', symbol).casefold())
            mechanism = found and any(word in found[1] for word in _CLASSES)
            return (0 if '[' in symbol or mechanism else 2 if symbol.startswith('_') else 1, symbol)
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
                unscoped = False
                if not examples and '.' in symbol:
                    examples = self.families.get(_family(symbol.split('.')[-1]), [])
                    unscoped = bool(examples)
                examples = [item for item in examples if item.get('source') != 'annotated_call'
                            or '.' in symbol or item.get('scope') in local_scopes]
                same_object = [item for item in examples if item['symbol'].casefold() == symbol.casefold()]
                examples = same_object or examples
                classes = set.intersection(*(set(item['mechanism_classes']) for item in examples)) if examples else set()
                if len(classes) == 1:
                    for example in examples[:2]:
                        add('annotated_mapping' if same_object else 'naming_analogy',
                            {'target': symbol, 'example_symbol': example['symbol'],
                            'mechanism_classes': sorted(classes), 'source_ref': example['id']},
                            caveat=('未限定声明作用域的同名族提示；' if unscoped else '') +
                                   '同命名族类别推断；仅可推断机构类别，不能照搬示例的方向、目标位和用途')
            chain = re.sub(r'\[\d+\]', '[N]', symbol)
            for pattern, word in self.wordbook.get('member_patterns', {}).items():
                if re.fullmatch(pattern, chain) or ('.' in chain and re.fullmatch(pattern, chain.split('.', 1)[1])):
                    add('wordbook_mapping', {'symbol': symbol, 'text': word, 'pattern': pattern})
                    break
            member = symbol.split('.')[-1]
            word = self.wordbook.get('members', {}).get(member)
            if word:
                add('wordbook_mapping', {'symbol': symbol, 'text': word})
            if len(evidence) >= 100:
                break
        packet = {'schema': SCHEMA, 'relation': relation,
                  'memory_file': MEMORY_FILE,
                  'source_fingerprint': self.fingerprint, 'evidence': evidence,
                  'required_actions': required,
                  'available_kinds': sorted(_KINDS),
                  'limitations': ['描述起始步；结束步注释和静态路径只作上下文',
                                 '调用实参、门控和映射可共同支持推断；机构同类命名只支持类别推断',
                                 '不得仅按步号大小、0值、工位名或相邻展示行猜动作']}
        packet['basis'] = stable_id('TB3', [start, end, packet])
        return packet


def _clause_refs(refs, catalog):
    if not isinstance(refs, list) or any(not isinstance(ref, str) or ref not in catalog for ref in refs):
        return None
    return [catalog[ref] for ref in dict.fromkeys(refs)]


def _clause_evidence(selected, step):
    """Separate names from evidence of an operation at the starting state."""
    local = [e for e in selected if e['local'] and e['category'] in {
        'current_command', 'guarded_call', 'bound_write', 'timer_alarm', 'start_comment', 'direct_control',
    }]
    facts, objects, asserted = [], [], set()
    symbols = set()
    for item in local:
        data = item['data']
        for key in ('arguments', 'guard', 'feedback', 'target'):
            symbols.update(s.casefold() for s in _symbols(data.get(key, '')))
        if item['category'] == 'start_comment':
            facts.append(data['text'])
        elif item['category'] == 'timer_alarm':
            facts.append(data['operation'])
        elif item['category'] in {'current_command', 'guarded_call', 'direct_control'}:
            facts.append(_comment_text(data.get('comment', ''), step))
            if item['category'] == 'current_command' and data['command'].casefold() == 'coil_set':
                asserted.add(data['arguments'].casefold())
        elif data.get('write') == 'coil_set':
            asserted.add(data['target'].casefold())
    for item in selected:
        data = item['data']
        name = data.get('symbol', data.get('target', '')).casefold()
        if name not in symbols:
            continue
        if item['category'] in {'exact_mapping', 'object_binding', 'wordbook_mapping'}:
            objects.append(data.get('label', data.get('text', '')))
        if item['category'] in {'naming_analogy', 'annotated_mapping'}:
            objects.extend(data.get('mechanism_classes', ()))
        if item['category'] == 'wordbook_mapping' and name in asserted:
            facts.append(data['text'])
    return bool(local), '\n'.join(facts), '\n'.join(objects)


def _validate_action_clauses(result, context):
    catalog = {item['id']: item for item in context['evidence']}
    required = set(context['required_actions'])
    actions = result.get('actions')
    if not isinstance(actions, list) or not actions:
        return None, 'B档须提交动作分句actions，逐项交代对象和动作'
    reasoning = result.get('reasoning')
    if result.get('kind') not in _KINDS or not isinstance(reasoning, str) or not reasoning.strip():
        return None, '须回传行性质和基于引用的推断说明reasoning'

    step = next(item['data']['from'] for item in catalog.values() if item['category'] == 'observed_pair')
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
            if not re.search(r'[\u4e00-\u9fff]', subject) or subject in _GENERIC_SUBJECTS:
                return None, '分句缺少执行机构、处理对象或具体条件'
            if not _VERB.search(action):
                return None, '分句缺少明确动作，机构名称或“动作、控制”不足以交付'
            local, facts, objects = _clause_evidence(selected, step)
            if not local:
                return None, '分句缺少本步指令、注释或控制证据'
            if any(token not in facts + objects for token in _CLASSES if token in subject):
                return None, '分句中的机构类别缺少本步绑定或中文映射'
            for detail in _SPECIFIC.findall(action + target):
                if detail not in facts:
                    return None, f'具体动作“{detail}”缺少同一步的指令效果或注释支持'
            target_name = re.sub(r'^(?:至|到)\s*', '', target)
            for detail in re.findall(r'\d+(?:\.\d+)?\s*(?:号位|毫秒|秒|分钟)|[\u4e00-\u9fff]{2,}(?:位置|工位|料位|安全位)', target_name):
                if detail not in facts:
                    return None, f'目标或时间“{detail}”缺少本步证据'
            phrase = subject + action + target
            for token in re.findall(r'[A-Za-z_][A-Za-z_0-9.\[\]]*', phrase):
                if not token.isupper() or (len(token) > 1 and token not in facts + objects):
                    return None, '分句含未映射的变量名或英文'
            phrases.append(phrase)
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
    if context.get('schema') == SCHEMA:
        return _validate_action_clauses(result, context)
    # Keep earlier saved v2 runs readable. A v3 packet has a different basis.
    refs = result.get('evidence_refs')
    catalog = {item['id']: item for item in context['evidence']}
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in catalog for ref in refs):
        return None, 'B档须引用当前行证据包中的有效证据ID'
    selected = [catalog[ref] for ref in dict.fromkeys(refs)]
    local = [e for e in selected if e['local'] and e['category'] != 'observed_pair']
    if not local:
        return None, '只有名称映射或步号，缺少本步动作、调用或控制证据'
    desc, reason = result.get('desc'), result.get('reasoning')
    if not isinstance(desc, str) or not desc.strip() or len(desc.strip()) > 40:
        return None, '描述须为1至40字中文'
    if not re.search(r'[\u4e00-\u9fff]', desc) or any(len(t) >= 2 for t in re.findall(r'[A-Za-z_][A-Za-z0-9_\[\].]*', desc)):
        return None, '描述含英文或变量名，或缺少中文'
    if result.get('kind') not in _KINDS or not isinstance(reason, str) or not reason.strip():
        return None, '须回传行性质和基于引用的推断说明reasoning'
    text, classes = '', ''
    for item in selected:
        if item['category'] == 'naming_analogy':
            classes += ' '.join(item['data'].get('mechanism_classes', []))
        if item['category'] in ('naming_analogy', 'neighbor_comment', 'observed_pair'):
            continue
        data = dict(item['data'])
        if item.get('comment_matches_step') is False:
            data.pop('comment', None)
        text += json.dumps(data, ensure_ascii=False)
        if data.get('command', '').casefold() in ('axismove', 'mc_moveabsolute', 'mc_moverelative', 'mc_movevelocity'):
            classes += '轴 电机 伺服'
    for mechanism in re.findall(r'气缸|机器人|真空|吸盘|伺服|电机', desc):
        if mechanism not in text + classes:
            return None, f'机构“{mechanism}”缺少引用的本步证据、中文映射或同类命名示例'
    for detail in re.findall(r'焊接|点胶|贴胶|伸出|缩回|上升|下降|夹紧|松开|正转|反转|到位|完成', desc):
        if detail not in text:
            return None, f'具体动作“{detail}”缺少所引用的本步证据或精确映射支持'
    level = '同类命名推断' if any(e['category'] == 'naming_analogy' for e in selected) else '上下文证据推断'
    return {'desc': desc.strip(), 'kind': result['kind'], 'refs': list(dict.fromkeys(refs)),
            'reasoning': reason.strip(), 'evidence_level': level}, ''
