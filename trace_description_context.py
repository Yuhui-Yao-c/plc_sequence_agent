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

MEMORY_FILE = 'trace_semantic_memory.json'
SCHEMA = 'trace-description-context-v2'
_SYMBOL = re.compile(r'[A-Za-z_][\w]*(?:\[\d+\])?(?:\.[A-Za-z_]\w*(?:\[\d+\])?)*')
_CLASSES = ('气缸', '伺服', '电机', '机器人', '吸盘', '真空', '定时器')
_KINDS = {'机械动作', '加工保持', '检测测量', '工艺等待', '交接数据', '周期汇总', '内部辅助', '信号/标志', '步进', '未分类'}


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
        for command in commands[:24]:
            c = _compact_command(command)
            prefix = re.match(r'^\s*(?:步\s*)?(\d+)\s*[-—:：>]', c['comment'])
            usable_comment = not prefix or int(prefix[1]) == number
            add('current_command', c, local=True, comment_matches_step=usable_comment)
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
            for key in ('arguments', 'guard', 'feedback', 'next_state'):
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
                  'available_kinds': sorted(_KINDS),
                  'limitations': ['描述起始步；结束步注释和静态路径只作上下文',
                                 '调用实参、门控和映射可共同支持推断；机构同类命名只支持类别推断',
                                 '不得仅按步号大小、0值、工位名或相邻展示行猜动作']}
        packet['basis'] = stable_id('TB2', [start, end, packet])
        return packet


def validate_context_result(result, context):
    """Check provenance/format and unsupported specifics; semantics stay inferred."""
    if not isinstance(result, dict) or result.get('basis') != context.get('basis'):
        return None, 'B档证据版本不匹配'
    if result.get('ok') is not True:
        return None, str(result.get('reason') or 'B档证据不足')
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
