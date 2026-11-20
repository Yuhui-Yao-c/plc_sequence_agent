import csv
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from plc_trace.capture.step_discovery import discover_source_writes
from plc_trace.capture.write_index import write_index
from plc_trace.capture.sysmac_discovery import _numbered_network_notes
from plc_trace.cli import main
from plc_trace.trace.action_branches import BranchIndex, Write
from tests.test_plcopen_trace_control import compare, contact, ladder, move, operand, program


SCENARIO = """CASE Phase OF
100: IF Need THEN Phase:=110; ELSIF Skip THEN Phase:=120; ELSE Phase:=200; END_IF;
110: Phase:=120;
200: IF Choice THEN Phase:=205; ELSE Phase:=210; END_IF;
205,210: Phase:=120;
120: Phase:=0;
0: IF Other THEN Phase:=300; END_IF;
300: Phase:=310;
END_CASE;"""


class ActionBranchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'line.xml'

    def source_body(self, body, globals_=''):
        self.source.write_text('<project>' + globals_ + program('Line', body) + '</project>')

    def st(self, code, globals_=''):
        self.source_body('<body><ST><![CDATA[' + code + ']]></ST></body>', globals_)

    def index(self):
        writes, unknown = [], {}
        def blocker(name, start, site):
            unknown.setdefault((name.casefold(), start), []).append(site)
        discover_source_writes(self.source, 'plcopen', lambda *x: writes.append(Write(*x)), blocker)
        return BranchIndex(writes, unknown)

    def project(self, a, b):
        index = self.index()
        anchor = ('phase', a, b)
        admitted, outcomes = index.project([anchor])
        return [(index.writes[i].start, index.writes[i].end) for i in admitted], outcomes[anchor]

    def test_unobserved_arm_continues_to_join_only(self):
        self.st(SCENARIO)
        rows, outcome = self.project(100, 110)
        self.assertEqual(outcome['state'], '分支已观测')
        self.assertEqual(set(rows), {(100,110), (100,120), (100,200), (200,205), (200,210), (205,120), (210,120)})
        self.assertNotIn((300,310), rows)
        self.assertEqual(self.project(300,310)[0], [(300,310)])

    def test_direct_and_multistep_candidates_compete(self):
        self.st(SCENARIO)
        rows, outcome = self.project(100,120)
        self.assertEqual(outcome['state'], '端点已见，分支待辨')
        self.assertNotIn('branch', outcome)
        self.assertIn((100,120), rows)

    def test_joins_and_external_entries_cut_off_a_private_arm(self):
        self.st(SCENARIO.replace('300: Phase:=310;', '300: Phase:=310; 310: Phase:=200;'))
        rows, _ = self.project(100,110)
        self.assertIn((100,200), rows)
        self.assertNotIn((200,205), rows)

    def test_distinct_case_and_writer_identity(self):
        self.st('''IF M THEN CASE Phase OF 10: IF A THEN Phase:=11; ELSE Phase:=12; END_IF; END_CASE;
                    ELSE CASE Phase OF 10: Phase:=13; END_CASE; END_IF;''')
        self.assertEqual(set(self.project(10,11)[0]), {(10,11), (10,12)})
        self.st('CASE Phase OF 10: IF A THEN Phase:=11; ELSE Phase:=11; END_IF; END_CASE;')
        rows, outcome = self.project(10,11)
        self.assertEqual(rows, [(10,11)] * 2)
        self.assertEqual(len(set(outcome['candidates'])), 2)
        self.assertNotIn('branch', outcome)

    def test_sequential_overwrite_and_dynamic_targets_are_unresolved(self):
        self.st('CASE Phase OF 10: IF A THEN Phase:=11; END_IF; IF B THEN Phase:=12; END_IF; END_CASE;')
        self.assertNotIn('branch', self.project(10,11)[1])
        self.st('CASE Phase OF 10: IF A THEN Phase:=11; ELSE Phase:=RecipeNext; END_IF; END_CASE;')
        self.assertEqual(self.project(10,11)[1]['state'], '动态写步目标待核')

    def test_private_retry_survives_and_ambiguous_duration_is_withheld(self):
        self.st('CASE Phase OF 10: IF A THEN Phase:=20; ELSE Phase:=15; END_IF; 15: Phase:=10; END_CASE;')
        rows, outcome = self.project(10,20)
        self.assertIn((15,10), rows)
        self.assertEqual(outcome['state'], '端点间可能重试')
        self.assertNotIn('branch', outcome)

    def test_typed_staging_and_self_relative_write(self):
        declarations = '<globalVars>' + ''.join(
            f'<variable name="{name}"><type><INT/></type></variable>' for name in ('Phase','Next')) + '</globalVars>'
        self.st('Next:=Phase; CASE Phase OF 100: IF A THEN Next:=Phase+10; ELSE Next:=120; END_IF; END_CASE; Phase:=Next;', declarations)
        index = self.index()
        self.assertEqual({(e.start,e.end) for e in index.writes.values()}, {(100,110),(100,120)})
        self.assertTrue(all(e.site['commit']['temporary'] == 'Next' for e in index.writes.values()))
        self.assertEqual(self.project(100,110)[1]['state'], '分支已观测')

    def test_ld_fork_requires_same_physical_selector(self):
        rungs = (operand(1,'Phase') + operand(2,'100') + compare(3,1,2)
                 + contact(4,[3],'Ready') + contact(5,[3],'Ready').replace('<contact ', '<contact negated="true" ')
                 + operand(6,'110') + operand(7,'120') + move(8,4,6,'Phase') + move(9,5,7,'Phase')
                 + compare(13,1,2) + operand(14,'999') + move(15,13,14,'Phase'))
        self.source_body(ladder(rungs))
        self.assertEqual(set(self.project(100,110)[0]), {(100,110),(100,120)})

    def test_unrecorded_fb_requires_concrete_caller_and_child_selector(self):
        callee = ('<pou name="Unit" pouType="functionBlock"><interface><inOutVars>'
                  '<variable name="Cursor"><type><INT/></type></variable></inOutVars></interface>'
                  '<body><ST>CASE Cursor OF 20: Cursor:=25; 300: Cursor:=310; END_CASE;</ST></body></pou>')
        rungs = (operand(1,'Phase') + operand(2,'100') + compare(3,1,2) + operand(4,'110') + move(5,3,4,'Phase')
                 + operand(6,'Child') + operand(7,'20') + compare(8,6,7).replace('refLocalId="0"', 'refLocalId="3"'))
        from tests.test_plcopen_trace_control import port
        writable = port('Cursor',6).replace('</variable>', '<addData><data><IsInout>true</IsInout></data></addData></variable>')
        rungs += '<block localId="9" typeName="Unit" instanceName="Slot"><inputVariables>' + port('EN',8) + writable + '</inputVariables><inOutVariables/></block>'
        self.source_body(ladder(rungs), callee)
        index = self.index()
        admitted, _ = index.project([('phase',100,110)])
        self.assertEqual({(index.writes[i].channel,index.writes[i].end) for i in admitted}, {('phase',110),('child',25)})

    def test_sysmac_network_note_requires_matching_step_and_unambiguous_channel(self):
        def write(ident, network, variable, step, text):
            return dict(id=ident, scope='Station', body=f'{network};line:42;node:2', variable=variable,
                        **{'from': step, 'network_comment': text})

        rows = [write('a', 1, 'Phase', 20, '10-松开\n20-夹紧'), write('b', 1, 'Phase', 30, '10-松开\n20-夹紧'),
                write('c', 2, 'Phase', 20, '20-顶升'), write('d', 2, 'Other', 20, '20-顶升'),
                write('e', 3, 'Phase', 20, '整个模组'), write('f', 4, 'Phase', 20, '20-伸出\n20-缩回')]
        self.assertEqual(_numbered_network_notes(rows), {'a': '20-夹紧'})

    def test_sysmac_runtime_array_write_preserves_other_station_transitions(self):
        def variable(name, kind):
            return f'<Variable name="{name}"><Type><TypeName>{kind}</TypeName></Type></Variable>'

        declarations = ''.join(variable(name, kind) for name, kind in
                               [('Phase', 'INT'), ('Buffer', 'ARRAY[0..9] OF INT'), ('Index', 'INT')])
        bodies = ('<BodyContent name="Station"><ST>CASE Phase OF 10: Phase:=20; END_CASE;</ST></BodyContent>'
                  '<BodyContent name="Log"><ST>Buffer[Index]:=Phase;</ST></BodyContent>'
                  '<BodyContent name="Lookup"><ST>Buffer[Buffer[0]]:=Phase;</ST></BodyContent>')
        self.source.write_text('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
                               + declarations + '</GlobalVars><Program name="Line">' + bodies + '</Program></Project>')
        writes = []
        discover_source_writes(self.source, 'omron_iec61131_10', lambda *args: writes.append(Write(*args)))
        self.assertEqual([(edge.channel, edge.start, edge.end) for edge in writes], [('phase', 10, 20)])

    def test_sysmac_repeated_call_on_one_line_keeps_mode_and_instance(self):
        def var(n, t):
            return f'<Variable name="{n}"><Type><TypeName>{t}</TypeName></Type></Variable>'
        def st(code):
            return '<BodyContent name="Cycle"><ST>' + code + '</ST></BodyContent>'
        self.source.write_text('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
            + var('Phase','INT') + '</GlobalVars><FunctionBlock name="Worker"><InOutVars>' + var('v','INT')
            + '</InOutVars>' + st('CASE v OF 10: v:=20; END_CASE;') + '</FunctionBlock><Program name="Site"><LocalVars>'
            + var('obj','Worker') + '</LocalVars>' + st('IF Mode THEN (* operator note *) obj(v:=Phase); ELSE obj(v:=Phase); END_IF;')
            + '</Program></Project>')
        writes = []
        discover_source_writes(self.source, 'omron_iec61131_10', lambda *x: writes.append(Write(*x)))
        self.assertEqual(len({e.id for e in writes}), 2)
        self.assertEqual({e.site['operating_guard'] for e in writes}, {'Mode', 'NOT (Mode)'})
        self.assertFalse(BranchIndex(writes).project([('phase',10,20)])[0])

    def test_normal_entry_retains_measurements_and_blank_branch_cells(self):
        self.st(SCENARIO)
        trace = self.root / '20260101080000_00001.csv'
        base = 1700000000000000000
        trace.write_text('TraceType,Continuous\nSampleTimingType,interval\nStartRawTime,' + str(base)
                         + '\nIndex,RawTime,Phase:INT\n' + ''.join(
                             f'{i},{base+10**7*i},{value}\n' for i, value in enumerate((99,100,100,110,120))))
        out = self.root/'run'
        argv = ['plc_cli.py','trace','--source',str(self.source),'--trace',str(trace),'--out',str(out)]
        with patch('sys.argv', argv), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            main()
        book = load_workbook(next(out.glob('*.xlsx')))
        sheet = book['时序拆解']
        rows = {(r[7],r[8]):r for r in sheet.values if r[7] and r[8]}
        self.assertEqual(rows['Phase=100','Phase=110'][5],20)
        self.assertIsNone(rows['Phase=100','Phase=120'][5])
        self.assertEqual(rows['Phase=200','Phase=205'][15],'本次未观测')
        self.assertNotIn(('Phase=300','Phase=310'),rows)
        self.assertEqual(book['实测跳变'].max_row,4)
        self.assertEqual(book['分支待核'].max_row,2)
        with (out/'trace_branch_events.csv').open(encoding='utf-8-sig') as stream:
            events = list(csv.DictReader(stream))
        self.assertEqual(len(events),3)
        self.assertEqual(sum(bool(r['分支ID']) for r in events),2)
        book.close()
        cached = out/'source_branch_index.json'
        copy = self.root/'second'
        copy.mkdir()
        with patch('plc_trace.capture.write_index.discover_source_writes', side_effect=AssertionError):
            self.assertTrue(write_index(self.source,copy,cached)['writes'])
        data = json.loads(cached.read_text())
        data['records']['writes'].clear()
        cached.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'校验'):
            write_index(self.source,copy,cached)
