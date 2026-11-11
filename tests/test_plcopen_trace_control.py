import json
from pathlib import Path
import tempfile
import unittest

from plc_trace.description.trace_description_context import validate_context_result
from plc_trace.trace.trace_source_evidence import TraceSourceIndex


def operand(node, value):
    return f'<inVariable localId="{node}"><expression>{value}</expression></inVariable>'


def port(name, node, output=''):
    select = f' formalParameter="{output}"' if output else ''
    return (f'<variable formalParameter="{name}"><connectionPointIn>'
            f'<connection refLocalId="{node}"{select}/></connectionPointIn></variable>')


def compare(node, first, second):
    return (f'<block localId="{node}" typeName="EQ"><inputVariables>'
            f'{port("EN", 0)}{port("", first)}{port("", second)}</inputVariables></block>')


def write_block(node, enable, operands, target, operation='MOVE'):
    data = ''.join(port('', value) for value in operands)
    return (f'<block localId="{node}" typeName="{operation}"><inputVariables>'
            f'{port("EN", enable)}{data}</inputVariables><outputVariables>'
            f'<variable formalParameter=""><connectionPointOut><expression>{target}</expression>'
            f'</connectionPointOut></variable></outputVariables></block>')


def move(node, enable, value, target):
    return write_block(node, enable, (value,), target)


def contact(node, enable, variable):
    return (f'<contact localId="{node}"><connectionPointIn>'
            + ''.join(f'<connection refLocalId="{source}"/>' for source in enable)
            + f'</connectionPointIn><variable>{variable}</variable></contact>')


def network(node, title):
    return (f'<comment><content>网络注释</content></comment><vendorElement localId="{node}">'
            f'<alternativeText>{title}</alternativeText>'
            '<addData><data><ElementType>networktitle</ElementType></data></addData></vendorElement>')


def ladder(nodes):
    return f'<body><LD><leftPowerRail localId="0"/>{nodes}</LD></body>'


def program(name, body):
    return f'<pou name="{name}" pouType="program">{body}</pou>'


class PlcopenTraceControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = Path(tmp.name)
        self.source = self.run / 'source.xml'

    def index(self, pous, variables, declarations=''):
        self.source.write_text(f'<project>{declarations}<types><pous>{pous}</pous></types></project>',
                               encoding='utf-8')
        return TraceSourceIndex(self.run, variables, source=self.source)

    def test_inout_template_is_rebound_per_instance_and_monitor_is_excluded(self):
        fb = (
            '<pou name="PositionUnit" pouType="functionBlock"><interface><inOutVars>'
            '<variable name="Phase"><type><INT/></type></variable>'
            '<variable name="Tool"><type><derived name="ToolControl"/></type></variable>'
            '</inOutVars></interface>'
            + ladder(network(100, '*10：夹具夹紧*') + operand(1, 'Phase') + operand(2, '10')
                     + compare(3, 1, 2)
                     + '<coil localId="4" storage="set"><connectionPointIn><connection refLocalId="3"/>'
                       '</connectionPointIn><variable>Tool.Close</variable></coil>'
                     + operand(5, '20') + move(6, 3, 5, 'Phase')
                     + network(101, '传递夹具状态') + operand(7, '20') + compare(8, 1, 7)
                     + operand(9, '30') + move(10, 8, 9, 'Phase'))
            + '</pou>'
        )
        calls = ''
        for offset, index in ((10, 1), (20, 2)):
            step_port = port('Phase', offset).replace(
                '</variable>', '<addData><data><IsInout>true</IsInout></data></addData></variable>')
            calls += operand(offset, f'Machine.Phase[{index}]') + operand(offset + 1, f'Fixtures[{index}]')
            calls += (f'<block localId="{offset + 2}" typeName="PositionUnit" instanceName="Slot{index}">'
                      f'<inputVariables>{port("EN", 0)}{step_port}{port("Tool", offset + 1)}</inputVariables>'
                      '<inOutVariables/></block>')
        controller = program('Line', ladder(calls))
        monitor = program('History', '<body><ST><![CDATA['
                          'Measured := (Machine.Phase[1]=10); //事后注释\n'
                          'IF Machine.Phase[2]=10 THEN Measured:=TRUE; END_IF;'
                          ']]></ST></body>')
        (self.run / 'step_comments.json').write_text(json.dumps({'Machine.Phase[1]=10': '事后注释'}))
        (self.run / 'indexed_events.csv').write_text(
            'guard,command,arguments,comment\nMachine.Phase[2]=30,Fake,Measured,事后注释\n', encoding='utf-8')
        variables = ('Machine.Phase[1]', 'Machine.Phase[2]')
        index = self.index(fb + controller + monitor, variables)
        for slot in (1, 2):
            label = f'Machine.Phase[{slot}]'
            found = index.lookup(label + '=10', label + '=30', '事后注释')
            self.assertEqual(found['description'], '10：夹具夹紧')
            self.assertEqual(found['relation'], 'path_candidate')
            self.assertEqual([edge['to'] for edge in found['candidate_path']], [20, 30])
            command, = found['start_commands']
            self.assertEqual(command['arguments'], f'Fixtures[{slot}].Close')
            self.assertEqual(command['instance_path'][0]['instance'], f'Slot{slot}')
        blank = index.lookup('Machine.Phase[2]=30', 'Machine.Phase[2]=10')
        self.assertNotIn('事后注释', json.dumps(blank, ensure_ascii=False))
        self.assertNotIn('Measured', json.dumps(blank))
        clean = self.index(fb + controller, variables)
        self.assertEqual(index.control.edges, clean.control.edges)
        self.assertEqual(index.control.notes, clean.control.notes)
        self.assertEqual(index.control.commands, clean.control.commands)

    def test_execute_inherits_ld_gate_and_feeds_b_tier_with_separate_write_and_condition(self):
        executable = (
            f'<block localId="8" typeName="EXECUTE"><inputVariables>{port("EN", 6, "Q")}</inputVariables>'
            '<addData><data><STCode><![CDATA['
            'IF FilterOn THEN Saved.Result:=Input.Value; Measured:=(Process.Phase[1]=10); '
            'Process.Phase[1]:=INT#20; '
            'ELSE Process.Phase[1]:=INT#30; END_IF;'
            ']]></STCode></data></addData></block>'
        )
        body = ladder(network(100, '网络注释') + operand(1, 'Process.Phase[1]') + operand(2, '10')
                      + compare(3, 1, 2)
                      + '<contact localId="4"><connectionPointIn><connection refLocalId="3"/>'
                        '</connectionPointIn><variable>Sensor.Stable</variable></contact>'
                      + operand(12, 'Tuning.Delta') + operand(13, '0.1')
                      + f'<block localId="14" typeName="&gt;="><inputVariables>'
                        f'{port("EN", 4)}{port("", 12)}{port("", 13)}</inputVariables></block>'
                      + f'<block localId="6" typeName="TON" instanceName="StableTimer">'
                        f'<inputVariables>{port("IN", 14)}</inputVariables>'
                        '<outputVariables><variable formalParameter="Q"/></outputVariables></block>'
                      + executable)
        (self.run / 'variable_documentation.json').write_text(json.dumps({'all': {'Q': '无关的设备'}}))
        index = self.index(program('Process', body), ('Process.Phase[1]',))
        info = index.lookup('Process.Phase[1]=10', 'Process.Phase[1]=20')
        self.assertEqual((info['relation'], info['description']), ('direct_unverified', ''))
        edge, = info['direct']
        self.assertEqual(edge['grade'], 'plcopen_structured_st')
        self.assertIn('Sensor.Stable', edge['condition_symbols'])
        self.assertIn('Tuning.Delta', edge['condition_symbols'])
        self.assertIn('FilterOn', edge['condition_symbols'])
        self.assertIn('StableTimer', edge['guard'])
        other = index.lookup('Process.Phase[1]=10', 'Process.Phase[1]=30')
        self.assertIn('NOT (FilterOn)', other['direct'][0]['guard'])
        self.assertEqual(other['linked_commands'], [])
        context = info['description_context']
        self.assertNotIn('无关的设备', json.dumps(context, ensure_ascii=False))
        condition, = [f for f in context['evidence'] if f['category'] == 'step_condition']
        command, = [f for f in context['evidence'] if f['category'] == 'current_command']
        self.assertEqual(command['data']['arguments'], 'Saved.Result')
        self.assertEqual(command['data']['write_value'], 'Input.Value')
        clause = {'subject': '检测反馈', 'action': '稳定', 'source_ref': condition['id'],
                  'subject_symbol': 'Sensor.Stable', 'condition_use': 'wait'}
        result = {'ok': True, 'kind': '工艺等待', 'basis': context['basis'],
                  'actions': [clause], 'reasoning': '稳态触点接在定时器输入，定时器输出使能写步'}
        accepted, issue = validate_context_result(result, context)
        self.assertFalse(issue)
        self.assertEqual(accepted['desc'], '等待检测反馈稳定')
        del clause['condition_use']
        self.assertIn('只支持等待或判断', validate_context_result(result, context)[1])
        clause.update(condition_use='check', subject_symbol='Sensor.Other')
        self.assertIn('不匹配', validate_context_result(result, context)[1])
        clause.update(subject='检测偏差', subject_symbol='Tuning.Delta', action='小于0.1')
        self.assertIn('数值判据', validate_context_result(result, context)[1])
        del clause['action']
        self.assertEqual(validate_context_result(result, context)[0]['desc'],
                         '判断检测偏差是否满足 ≥0.1')

    def test_st_original_branch_comment_and_unanchored_bypass(self):
        body = ('<body><ST><![CDATA[CASE Drive.Phase[2] OF\n'
                '10: //转送检测数据\n Data.Result:=Camera.Result; Drive.Phase[2]:=20;\n'
                '20: //等待对侧许可\n IF Ready THEN Drive.Phase[2]:=30; END_IF;\n'
                'END_CASE;\n'
                'IF Drive.Phase[2]=99 OR Bypass THEN //全局退出\n'
                'Drive.Phase[2]:=0; END_IF;]]></ST></body>')
        index = self.index(program('Drive', body), ('Drive.Phase[2]',))
        info = index.lookup('Drive.Phase[2]=10', 'Drive.Phase[2]=30')
        self.assertEqual(info['description'], '转送检测数据')
        note, = info['start_notes']
        self.assertEqual(note['st_line'], 2)
        self.assertEqual(note['origin'], 'plcopen_structured_st')
        info = index.lookup('Drive.Phase[2]=99', 'Drive.Phase[2]=0')
        self.assertEqual(info['relation'], 'unresolved')
        self.assertEqual(info['description'], '')

    def test_typed_arithmetic_and_unresolved_destination_keep_own_source_evidence(self):
        name = 'PhaseTable[2]'
        decl = ('<variable name="PhaseTable"><type><array><dimension lower="0" upper="3"/>'
                '<baseType><USINT/></baseType></array></type></variable>')
        declarations = f'<globalVars>{decl}<addData><data><globalVars>{decl}</globalVars></data></addData></globalVars>'
        body = ladder(
            network(100, '切换分支\n37 对接载具\n200 放行载具')
            + operand(1, name) + operand(2, '37') + compare(3, 1, 2)
            + operand(4, 'USINT#10') + write_block(5, 3, (1, 4), name, '+')
            + network(101, '置位移载许可') + operand(6, '250') + compare(7, 1, 6)
            + '<coil localId="8" storage="set"><connectionPointIn><connection refLocalId="7"/>'
              '</connectionPointIn><variable>Transfer.Permit</variable></coil>'
            + write_block(9, 7, (1, 4), name, 'ADD')
        )
        index = self.index(program('TransferUnit', body), (name,), declarations)
        info = index.lookup(f'{name}=37', f'{name}=47')
        self.assertEqual((info['relation'], info['description']), ('direct', '37 对接载具'))
        self.assertIn('200 放行载具', info['start_notes'][0]['raw'])
        self.assertEqual(index.station(name), 'TransferUnit')
        overflow = index.lookup(f'{name}=250', f'{name}=4')
        self.assertEqual((overflow['relation'], overflow['description']), ('unresolved', '置位移载许可'))
        self.assertIsNone(overflow['start_state_writes'][0]['to'])
        self.assertEqual(overflow['linked_commands'][0]['arguments'], 'Transfer.Permit')

    def test_parallel_guard_retains_shared_start_constraint(self):
        name = 'Procedure.Sequence'
        nodes = network(100, '对侧工位联锁完成') + operand(1, name) + operand(2, '21') + compare(3, 1, 2)
        left, right = 3, 3
        for layer in range(12):
            next_left, next_right = 10 + layer * 2, 11 + layer * 2
            nodes += contact(next_left, (left, right), f'PermitA[{layer}]')
            nodes += contact(next_right, (left, right), f'PermitB[{layer}]')
            left, right = next_left, next_right
        nodes += contact(40, (left, right), 'Control.Run') + operand(41, '93') + move(42, 40, 41, name)
        info = self.index(program('Transfer', ladder(nodes)), (name,)).lookup(f'{name}=21', f'{name}=93')
        self.assertEqual((info['relation'], info['description']), ('direct', '对侧工位联锁完成'))
        self.assertIn('(PermitA[11] OR PermitB[11])', info['direct'][0]['guard'])

    def test_wired_command_can_precede_exit_feedback_in_same_step(self):
        name = 'Sequence'
        ready = contact(4, (3,), 'Drive.InPosition')
        pending = ready.replace('localId="4"', 'localId="5" negated="true"')
        nodes = (network(100, '升降轴定位') + operand(1, name) + operand(2, '30') + compare(3, 1, 2)
                 + ready + pending + operand(6, '45') + move(7, 4, 6, name)
                 + '<coil localId="8" storage="set"><connectionPointIn><connection refLocalId="5"/>'
                   '</connectionPointIn><variable>Drive.PositionRequest</variable></coil>')
        info = self.index(program('Transfer', ladder(nodes)), (name,)).lookup(f'{name}=30', f'{name}=45')
        self.assertEqual(info['relation'], 'direct')
        self.assertEqual(info['linked_commands'][0]['arguments'], 'Drive.PositionRequest')

    def test_custom_bool_output_follows_declared_input_through_implicit_port(self):
        name = 'Sequence[0]'
        template = (
            '<pou name="AccumulatedWindow" pouType="functionBlock"><interface>'
            '<inputVars><variable name="Gate"><type><BOOL/></type></variable></inputVars>'
            '<outputVars><variable name="Elapsed"><type><BOOL/></type></variable></outputVars>'
            '</interface><body><ST><![CDATA[Elapsed := Gate AND (_Clock >= (Began + Duration));]]></ST></body>'
            '</pou>'
        )
        call = (f'<block localId="4" typeName="AccumulatedWindow" instanceName="Window[1]">'
                f'<inputVariables>{port("Gate", 3)}</inputVariables>'
                '<outputVariables><variable formalParameter="Elapsed"><addData><data>'
                '<IsMainOutput>true</IsMainOutput></data></addData></variable></outputVariables></block>')
        body = ladder(network(100, '延时放行') + operand(1, name) + operand(2, '25') + compare(3, 1, 2)
                      + call + operand(5, '56') + move(6, 4, 5, name))
        info = self.index(template + program('Transfer', body), (name,)).lookup(f'{name}=25', f'{name}=56')
        self.assertEqual((info['relation'], info['description']), ('direct_unverified', '延时放行'))
        dependency, = info['direct'][0]['output_dependencies']
        self.assertEqual(dependency['instance'], 'Window[1]')
        self.assertEqual(dependency['required_inputs'][0]['guard'], f'{name}=25')
        bypass = template.replace('Gate AND', 'Gate OR')
        info = self.index(bypass + program('Transfer', body), (name,)).lookup(f'{name}=25', f'{name}=56')
        self.assertEqual((info['relation'], info['description']), ('unresolved', ''))

    def test_st_self_relative_assignment_uses_declared_type_and_exact_branch(self):
        body = (
            '<interface><localVars><variable name="Mode"><type><DINT/></type></variable></localVars></interface>'
            '<body><ST><![CDATA[CASE Mode OF\n'
            '21: //进入下一组\n Mode:=Mode + DINT#7;\n'
            '40: //重排步值\n Mode:=42; Mode:=Mode - 1;\n'
            'END_CASE;]]></ST></body>'
        )
        index = self.index(program('Exchange', body), ('Mode',))
        self.assertEqual(index.lookup('Mode=21', 'Mode=28')['relation'], 'direct')
        info = index.lookup('Mode=40', 'Mode=39')
        self.assertEqual(info['relation'], 'unresolved')
        self.assertEqual([write['to'] for write in info['start_state_writes']], [42, None])


if __name__ == '__main__':
    unittest.main()
