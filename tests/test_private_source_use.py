import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.coverage import _parent_entries
from plc_trace.capture.extract_step_variables import generate
from tests.test_capture_activation import project, section, variable


class PrivateSourceUseTests(unittest.TestCase):
    def prepare(self, extra=''):
        # Leading blank lines, same-named formals, two gated FB calls, and
        # several statements on one line all occur in controller exports.
        code = """
        CASE Owner OF
          0: Owner:=1;
          1: IF Active THEN Mark:=7; Owner:=2; END_IF;
          2: IF Mark=9 THEN Owner:=3; END_IF;
          3: Owner:=0;
        END_CASE;
        CASE Mark OF 7: IF Ready THEN Mark:=8; END_IF; 8: Mark:=9; END_CASE;
        """ + extra
        fb = ('<FunctionBlock name="Clamp"><InOutVars>' + variable('Owner','INT') + '</InOutVars>'
              + '<Vars accessSpecifier="private">' + variable('Mark','INT') + '</Vars>'
              + section(code).replace('<BodyContent>', '<BodyContent name="Run" evaluationOrder="3">')
              + '</FunctionBlock>')
        host = ('<Program name="Site"><Vars>' + variable('Left','Clamp') + variable('Right','Clamp') + '</Vars>'
                + section('IF ModeA THEN Left(Owner:=A); END_IF; IF ModeB THEN Right(Owner:=B); END_IF;')
                + '</Program>')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        src = root / 'cell.xml'
        src.write_text(project('<GlobalVars>'+variable('A','INT')+variable('B','INT')+'</GlobalVars>', fb, host))
        generate(src, root / 'out')
        inventory = json.loads((root / 'out' / 'step_variable_discovery.json').read_bytes())
        return {row['name']:row for row in inventory['candidates']}

    def test_private_launch_and_completion_correlate_to_concrete_invocation(self):
        rows = self.prepare()
        for instance, actual in [('Left','A'), ('Right','B')]:
            row = rows['Site.'+instance+'.Mark']
            start, = _parent_entries(row)
            self.assertEqual(start['parent'], actual)
            self.assertEqual((start['binding'][0]['instance'], start['parent_step']), (instance, 1))
            self.assertIn('Owner=1', start['local_guard'])
            self.assertIn(actual+'=1', start['guard'])
            reads = row['activation']['read_uses']
            self.assertCountEqual([r['kind'] for r in reads], ['dispatch', 'parent_exit'])
            exit_test = next(r for r in reads if r['kind']=='parent_exit')
            complete = next(l for l in row['control_links'] if l['kind']=='waits_for_child')
            self.assertEqual((exit_test['write_offset'],exit_test['target'],exit_test['value'],exit_test['to']),
                             (complete['offset'],complete['selector'],complete['child_value'],3))
            # Another section at the same source line, value and offset cannot attest this write.
            self.assertFalse(_parent_entries({**row,'control_links':[{**start, 'scope':'Clamp::Another'}]}))
            self.assertFalse(_parent_entries({**row,'control_links':[{**start, 'offset':start['offset']+1}]}))
            unindexed = {k:v for k,v in row['activation']['entries'][0].items() if k != 'scope'}
            self.assertFalse(_parent_entries({**row,'activation':{**row['activation'], 'entries':[unindexed]}}))
            other = 'Right' if instance=='Left' else 'Left'
            foreign = _parent_entries(rows['Site.'+other+'.Mark'])[0]
            self.assertFalse(_parent_entries({**row,'control_links':[{**start, 'binding':foreign['binding']}]}))

    def test_referenced_intermediate_state_and_complex_consumers_remain_visible(self):
        variants = ('Audit := Mark;', 'IF Mark=8 THEN Notify:=1; Alarm:=2; END_IF;',
                    'CASE Mark OF 8: Notify:=1; END_CASE;', 'IF OK AND Mark=8 THEN Notify:=1; END_IF;')
        for other in variants:
            with self.subTest(other=other):
                reads = self.prepare(other)['Site.Left.Mark']['activation']['read_uses']
                self.assertCountEqual([r['kind'] for r in reads], ['dispatch','parent_exit','other'])
