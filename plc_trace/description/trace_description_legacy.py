"""Read saved Trace v2/v3 result formats with their original checks."""
import json
import re

from plc_trace.trace.trace_source_evidence import _comment_text

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



def _clause_evidence(selected, step, symbols_for):
    """Separate names from evidence of an operation at the starting state."""
    local = [e for e in selected if e['local'] and e['category'] in {
        'current_command', 'guarded_call', 'bound_write', 'timer_alarm', 'start_comment', 'direct_control',
    }]
    facts, objects, asserted = [], [], set()
    symbols = set()
    for item in local:
        data = item['data']
        for key in ('arguments', 'guard', 'feedback', 'target'):
            symbols.update(s.casefold() for s in symbols_for(data.get(key, '')))
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


def check_v3_phrase(subject, action, target, selected, step, symbols_for):
    if not re.search(r'[\u4e00-\u9fff]', subject) or subject in _GENERIC_SUBJECTS:
        return '分句缺少执行机构、处理对象或具体条件'
    if not _VERB.search(action):
        return '分句缺少明确动作，机构名称或“动作、控制”不足以交付'
    local, facts, objects = _clause_evidence(selected, step, symbols_for)
    if not local:
        return '分句缺少本步指令、注释或控制证据'
    if any(token not in facts + objects for token in _CLASSES if token in subject):
        return '分句中的机构类别缺少本步绑定或中文映射'
    for detail in _SPECIFIC.findall(action + target):
        if detail not in facts:
            return f'具体动作“{detail}”缺少同一步的指令效果或注释支持'
    target_name = re.sub(r'^(?:至|到)\s*', '', target)
    for detail in re.findall(r'\d+(?:\.\d+)?\s*(?:号位|毫秒|秒|分钟)|[\u4e00-\u9fff]{2,}(?:位置|工位|料位|安全位)', target_name):
        if detail not in facts:
            return f'目标或时间“{detail}”缺少本步证据'
    phrase = subject + action + target
    for token in re.findall(r'[A-Za-z_][A-Za-z_0-9.\[\]]*', phrase):
        if not token.isupper() or (len(token) > 1 and token not in facts + objects):
            return '分句含未映射的变量名或英文'
    return ''


def validate_v2_result(result, context):
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
