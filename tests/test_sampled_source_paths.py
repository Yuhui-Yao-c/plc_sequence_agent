import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.write_index import write_index
from plc_trace.trace.action_branches import BranchIndex, Write
from tests.test_plcopen_trace_control import program


def incoming(*ports):
    return '<ConnectionPointIn>'+''.join(f'<Connection refConnectionPointOutId="{p}"/>' for p in ports)+'</ConnectionPointIn>'


def outgoing(port):
    return f'<ConnectionPointOut connectionPointOutId="{port}"/>'


def element(kind, attrs='', body=''):
    return f'<Object xsi:type="{kind}" {attrs}>{body}</Object>'


def operand(value, port):
    return element('DataSource',f'identifier="{value}"',outgoing(port))


def block(op, inputs, outputs, attrs='', inout=''):
    ins=''.join(f'<InputVariable parameterName="{p}">{incoming(*rs)}</InputVariable>' for p,rs in inputs)
    outs=''.join(f'<OutputVariable parameterName="{p}">{outgoing(r)}</OutputVariable>' for p,r in outputs)
    return element('Block',f'typeName="{op}" {attrs}',f'<InputVariables>{ins}</InputVariables><OutputVariables>{outs}</OutputVariables>'+inout)


def rung(order, start, end, middle='', enables=(4,)):
    return (f'<Rung evaluationOrder="{order}">'+element('LeftPowerRail',body=outgoing(1))
            +operand('Cursor',2)+operand(start,3)+block('=',[('EN',[1]),('In1',[2]),('In2',[3])],[('',4)])
            +middle+operand(end,28)+block('MOVE',[('EN',enables),('In',[28])],[('Out',30)])
            +element('DataSink','identifier="Cursor"',incoming(30))+'</Rung>')


def variable(name,typ):
    return f'<Variable name="{name}"><Type><TypeName>{typ}</TypeName></Type></Variable>'


class SampledSourceTests(unittest.TestCase):
    def model(self,body,fb='',plcopen=False):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        folder=Path(temp.name);source=folder/'line.xml'
        if plcopen:
            xml='<project>'+body+'</project>'
        else:
            xml=('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                 +'<GlobalVars>'+variable('Cursor','INT')+variable('Parent','INT')+'</GlobalVars>'
                 +fb+'<Program name="Cell">'+body+'</Program></Project>')
        source.write_text(xml)
        data=write_index(source,folder)
        entries={}
        for v,t,info in data['entries']:entries.setdefault((v.casefold(),t),[]).append(info)
        return BranchIndex((Write(*row) for row in data['writes']),entries=entries)

    @staticmethod
    def ld(content):
        return '<BodyContent name="Cycle">'+content+'</BodyContent>'

    def test_parallel_constants_and_timed_output_preserve_observed_span(self):
        control=element('Contact','operand="1"',incoming(4)+outgoing(7))
        clock=block('Timer',[('In',[4]),('PT',[8])],[('',9)],
                    inout='<InOutVariables><InOutVariable parameterName="TimerDat">'+incoming(7)+'</InOutVariable></InOutVariables>')
        index=self.model(self.ld(rung(6,31,57,control,(7,))+rung(7,57,81,operand('State',7)+operand('Preset',8)+clock,(9,))
                                  +rung(8,303,401)))
        key=('cursor',31,81);admitted,states=index.project([key]);state=states[key]
        self.assertEqual(state['origin']['routes'][0]['steps'],[31,57,81])
        self.assertEqual(state['state'],'端点间存在中间步')
        self.assertNotIn('branch',state)
        self.assertEqual({(index.writes[i].start,index.writes[i].end) for i in admitted},{(31,57),(57,81)})
        timer=next(e for e in index.writes.values() if e.start==57)
        self.assertFalse(timer.complete)
        self.assertEqual(timer.site['dependencies'][0]['kind'],'timer_return')

    def test_unmodelled_comparison_requires_dominating_selector(self):
        # The upstream comparison reads a computed output, whose data value is opaque.
        condition=(operand('Data',6)+block('Compute',[],[('Out',8)])+
                   block('>',[('EN',[4]),('In1',[8]),('In2',[6])],[('',9)]))
        index=self.model(self.ld(rung(1,21,22,condition,(9,))+rung(2,35,36,condition,(9,1))))
        self.assertEqual({(e.start,e.end) for e in index.writes.values()},{(21,22)})
        edge=next(iter(index.writes.values()))
        self.assertFalse(edge.complete)
        self.assertEqual(edge.site['dependencies'][0]['kind'],'unresolved_control')

    def test_fb_output_must_be_written_and_require_the_control_input(self):
        fb=('<FunctionBlock name="SensorCheck"><Parameters><InputVars>'+variable('Enable','BOOL')+'</InputVars><OutputVars>'
            +variable('Ready','BOOL')+'</OutputVars></Parameters><BodyContent><Rung evaluationOrder="1">'
            +element('LeftPowerRail',body=outgoing(1))+element('Contact','operand="Enable"',incoming(1)+outgoing(2))
            +element('Contact','operand="Feedback"',incoming(2)+outgoing(3))+element('Coil','operand="Ready"',incoming(3))+'</Rung></BodyContent></FunctionBlock>')
        call=block('SensorCheck',[('Enable',[4])],[('Ready',8)],'instanceName="Detector"')
        body=self.ld(rung(7,50,55,call,(8,)))
        index=self.model(body,fb)
        edge=next(iter(index.writes.values()))
        self.assertEqual(edge.site['dependencies'][0]['definition']['inputs'],['Enable'])
        self.assertFalse(self.model(body,fb.replace('operand="Ready"','latch="set" operand="Ready"')).writes)
        # The name _EN does not prove it gates this user-defined FB output.
        unrelated=fb.replace('</InputVars>',variable('_EN','BOOL')+'</InputVars>')
        call=block('SensorCheck',[('_EN',[4]),('Enable',[1])],[('Ready',8)],'instanceName="Detector"')
        self.assertFalse(self.model(self.ld(rung(7,50,55,call,(8,))),unrelated).writes)

    def test_return_to_earlier_rung_has_no_timed_or_unrelated_branch(self):
        idx=self.model(self.ld(rung(14,45,3)+rung(2,3,9)+rung(3,9,83)+rung(8,35,36)))
        key=('cursor',45,83);admitted,states=idx.project([key])
        self.assertFalse(admitted)
        self.assertEqual(states[key]['origin']['kind'],'返回前序梯级')
        self.assertNotIn('branch',states[key])

    def test_handoff_requires_unconditional_calls_in_same_program(self):
        def action(name,body):
            return f'<action name="{name}"><body><ST>{body}</ST></body></action>'
        def line(dispatch):
            actions=action('Prepare','CASE Cursor OF 12: Cursor:=43; END_CASE;')+action('Unload','CASE Cursor OF 43: Cursor:=76; END_CASE;')
            return program('Line','<actions>'+actions+'</actions><body><ST>'+dispatch+'</ST></body>')
        key=('cursor',12,76)
        idx=self.model(line('Prepare(); Unload();'),plcopen=True)
        admitted,states=idx.project([key]);self.assertFalse(admitted)
        self.assertEqual(states[key]['origin']['routes'][0]['steps'],[12,43,76])
        self.assertEqual(states[key]['state'],'跨动作段接续')
        for code in ('Prepare();','Unload(); Prepare();','IF On THEN Prepare(); END_IF; Unload();'):
            self.assertEqual(self.model(line(code),plcopen=True).project([key])[1][key]['origin']['kind'],'来源待核')

    def test_external_writer_after_loop_is_not_a_local_edge(self):
        text='''CASE Parent OF 11: FOR N:=1 TO 4 DO Run(N); END_FOR; END_CASE;
                CASE Parent OF 15: CASE N OF 2..7: Flag:=TRUE; END_CASE; END_CASE;
                IF Active THEN CASE Parent OF 42: Cursor:=10; END_CASE; END_IF;
                CASE Cursor OF 10: Cursor:=80; END_CASE;'''
        idx=self.model('<BodyContent name="Supervisor"><ST>'+text+'</ST></BodyContent>'
                       +self.ld(rung(1,10,80)))
        key=('cursor',9,10);admitted,states=idx.project([key])
        self.assertFalse(admitted)
        self.assertEqual(states[key]['state'],'其他逻辑写入终值')
        entry=states[key]['origin']['entry_writers'][0]
        self.assertIn('Parent=42',entry['guard'])
        self.assertNotIn('Cursor=9',entry['guard'])
        self.assertFalse(entry['guard_complete'])

    def test_range_write_competes_only_on_covered_array_elements(self):
        decl='<GlobalVars>'+variable('Slot','ARRAY[0..8] OF INT')+'</GlobalVars>'
        fill=(element('LeftPowerRail',body=outgoing(1))+operand(2,2)+operand(0,3)+operand('Slot[3]',4)
              +block('SetBlock',[('EN',[1]),('Size',[2]),('In',[3])],[],
                     inout='<InOutVariables><InOutVariable parameterName="AryOut">'+incoming(4)+'</InOutVariable></InOutVariables>'))
        body=self.ld('<Rung evaluationOrder="1">'+fill+'</Rung>'+
                     ''.join((rung(4,7,0)+rung(5,0,6)).replace('Cursor',f'Slot[{i}]') for i in (2,3,4,5)))
        index=self.model(body,decl)
        for i in (2,3,4,5):
            key=(f'slot[{i}]',7,6)
            admitted,states=index.project([key])
            expected=i in (3,4)
            self.assertEqual(bool(states[key]['origin']['intermediate_writers']),expected)
            self.assertEqual(bool(admitted),not expected)
