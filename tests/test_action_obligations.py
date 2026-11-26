import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.action_obligations import action_obligation
from plc_trace.capture.extract_step_variables import generate
from tests.test_capture_activation import project, section, variable


MOVE = """IF Running THEN
CASE Position OF
  7: IF NOT X.AtTarget THEN X.Request:=TRUE; END_IF;
     IF X.AtTarget THEN Position:=64; END_IF;
 64: Position:=3;
  3: Y.Request:=TRUE; IF Y.AtTarget THEN Position:=7; END_IF;
END_CASE;
END_IF;"""


class ActionObligationTests(unittest.TestCase):
    def row(self, code=MOVE):
        device = ('<DataTypeDecl name="Servo">' +
                  ''.join('<Member name="' + s + '"><Type><TypeName>BOOL</TypeName></Type></Member>'
                          for s in ['Request', 'AtTarget']) + '</DataTypeDecl>')
        program = ('<Program name="Cell">' + section(code) + '</Program>')
        fields = [variable(s, 'Servo') for s in ('X', 'Y', 'Z')]
        fields += [variable(s, 'BOOL') for s in ('Running', 'Allow', 'Bypass')]
        fields += [variable('Position', 'INT')]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base/'machine.xml'
            source.write_text(project(device, '<GlobalVars>' + ''.join(fields) + '</GlobalVars>', program))
            generate(source, base/'run')
            inventory = json.loads((base/'run/evidence/step_variable_discovery.json').read_bytes())
        return next(r for r in inventory['candidates'] if r['identity'] == 'position')

    def test_connected_stages_can_use_direct_or_complementary_feedback(self):
        row = self.row()
        proof = action_obligation(row)
        stages = proof['modes'][0]['stages']
        self.assertEqual({(s['from'], s['to'], s['command'], s['feedback']) for s in stages},
                         {(7, 64, 'x.request', 'x.attarget'), (3, 7, 'y.request', 'y.attarget')})
        for s in stages:
            self.assertIn(s['write'], {w['id'] for w in row['transitions']})
            self.assertIn(s['effect'], {e['id'] for e in row['effects']})
        self.assertIsNone(action_obligation(dict(row, activation={'open_lines': []})))
        self.assertIsNotNone(action_obligation(dict(row, activation={'open_lines': [20]})))

    def test_foreign_optional_disconnected_or_incompatible_feedback_does_not_qualify(self):
        cases = (
            MOVE.replace('IF Y.AtTarget', 'IF Z.AtTarget'),
            MOVE.replace('IF Y.AtTarget', 'IF Y.AtTarget OR Bypass'),
            MOVE.replace('64: Position:=3;', '64: Position:=71;').replace('Position:=7;', 'Position:=99;'),
            MOVE.replace('IF NOT X.AtTarget', 'IF Allow AND NOT X.AtTarget')
                .replace('IF X.AtTarget', 'IF NOT Allow AND X.AtTarget'),
            MOVE.replace('IF NOT X.AtTarget THEN X.Request:=TRUE;', 'IF NOT X.AtTarget THEN Y.Request:=TRUE;'),
        )
        for source in cases:
            with self.subTest(code=source):
                self.assertIsNone(action_obligation(self.row(source)))

    def test_reused_step_values_across_modes_do_not_form_job(self):
        source = """IF Running THEN CASE Position OF 7: X.Request:=TRUE;
          IF X.AtTarget THEN Position:=3; END_IF; 3: Position:=7; END_CASE;
          ELSE CASE Position OF 3: Y.Request:=TRUE; IF Y.AtTarget THEN Position:=7; END_IF;
          7: Position:=3; END_CASE; END_IF;"""
        self.assertIsNone(action_obligation(self.row(source)))
