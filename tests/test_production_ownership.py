import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.capture_use import capture_context, capture_lists
from plc_trace.capture.coverage import _parent_entries
from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.production_ownership import enclosed_child
from plc_trace.capture.step_discovery import scope_selection
from tests.test_capture_activation import project, variable


MAIN = """CASE Process OF
 17: Fine:=4; Move:=TRUE; IF Arrived THEN Process:=8; END_IF;
 8: IF Clamped THEN Process:=51; END_IF;
 51: IF Fine=73 THEN Process:=9; END_IF;
 9: Clamp:=FALSE; Grip:=FALSE; Process:=17;
 END_CASE;
 CASE Fine OF
 4: Clamp:=TRUE; IF Near THEN Fine:=62; END_IF;
 62: Grip:=TRUE; IF Seated THEN Fine:=73; END_IF;
 END_CASE;"""


class ProductionOwnershipTests(unittest.TestCase):
    def inventory(self, code=MAIN, *, children=('FixtureA',), caller_extra=''):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        globals_ = [variable(n, 'BOOL') for n in ('Move', 'Arrived', 'Clamped', 'Clamp', 'Near', 'Grip', 'Seated', 'Delivered')]
        globals_ += [variable('Owner' + name, 'INT') for name in children]
        globals_ += [variable('Workpiece', 'DINT'), variable('Display', 'STRING')]
        fb = ('<FunctionBlock name="Worker"><InOutVars>' + variable('Process', 'INT') + '</InOutVars>'
              '<Vars accessSpecifier="private">' + variable('Fine', 'INT') + '</Vars>'
              '<BodyContent name="Advance" evaluationOrder="1"><ST><![CDATA[' + code +
              ']]></ST></BodyContent></FunctionBlock>')
        calls = ' '.join(f'{n}(Process:=Owner{n});' for n in children)
        program = ('<Program name="Bay"><Vars>' + ''.join(variable(n, 'Worker') for n in children) +
                   '</Vars><BodyContent name="Run"><ST>' + calls + caller_extra + '</ST></BodyContent></Program>')
        xml = root / 'fixture.xml'
        xml.write_text(project('<GlobalVars>' + ''.join(globals_) + '</GlobalVars>', fb, program))
        generate(xml, root / 'run')
        data = json.loads((root / 'run/step_variable_discovery.json').read_bytes())
        return {r['identity']: r for r in data['candidates']}

    @staticmethod
    def proof(rows, local='Bay.FixtureA.Fine'):
        row = rows[local.casefold()]
        return enclosed_child(row, rows, _parent_entries(row))

    def test_source_connects_private_launch_and_join_to_real_owner_not_the_step_number_order(self):
        rows = self.inventory()
        proof = self.proof(rows)
        self.assertEqual((proof['parent'], proof['start_step'], proof['wait_step']), ('OwnerFixtureA', 17, 51))
        owner = rows[proof['parent'].casefold()]
        writes = {w['id']: (w['from'], w['to']) for w in owner['transitions']}
        self.assertEqual([writes[wid] for wid in proof['parent_writes']], [(17, 8), (8, 51), (51, 9)])
        self.assertEqual(writes[proof['parent_exit_write']], (51, 9))
        self.assertEqual({p['child_output'] for p in proof['handbacks']}, {'clamp', 'grip'})

    def test_independent_consumers_and_incomplete_rendezvous_prevent_exclusion(self):
        cases = {
            'explicit intermediate handoff': MAIN + ' IF Fine=62 THEN Delivered:=TRUE; END_IF;',
            'separate material handoff without exposing cursor': MAIN.replace('Grip:=TRUE;', 'Grip:=TRUE; Delivered:=TRUE;'),
            'publishes a workpiece number': MAIN.replace('Grip:=TRUE;', 'Grip:=TRUE; Workpiece:=42;'),
            'calls another block': MAIN.replace('Grip:=TRUE;', 'Grip:=TRUE; Receiver(Ident:=42);'),
            'calls in condition': MAIN.replace('IF Near THEN', 'IF Near AND Receiver() THEN'),
            'opaque call on terminal arm': MAIN.replace('Fine:=73; END_IF;', 'Fine:=73; END_IF; 73: Receiver();'),
            'unrelated string output': MAIN.replace('Grip:=TRUE;', "Grip:=TRUE; Display:='Transferred';"),
            'parent takes back only one device': MAIN.replace('Grip:=FALSE;', ''),
            'timing consumer': MAIN + ' TimerInput:=Fine;',
            'additional entry': MAIN + ' IF Demand THEN Fine:=4; END_IF;',
            'alternate exit': MAIN.replace('Process:=9; END_IF;', 'Process:=9; ELSIF Aborted THEN Process:=0; END_IF;'),
            'wrapper only': MAIN.replace('Move:=TRUE; IF Arrived THEN Process:=8; END_IF;', 'Process:=51;'),
            'independent child branch': MAIN.replace('Fine:=62; END_IF;', 'Fine:=62; ELSIF Bypassed THEN Fine:=73; END_IF;'),
        }
        for scenario, code in cases.items():
            with self.subTest(scenario=scenario):
                self.assertIsNone(self.proof(self.inventory(code)))

    def test_stage_caption_must_be_the_same_parent_caption_field(self):
        annotated = MAIN.replace('17: Fine:=4;', "17://Lift\n Display:='Lift'; Fine:=4;").replace(
            '8: IF Clamped', "8://Move\n Display:='Move'; IF Clamped")
        child = '62://Grip\n Display:=\'Grip\'; Grip:=TRUE;'
        self.assertIsNotNone(self.proof(self.inventory(annotated.replace('62: Grip:=TRUE;', child))))
        self.assertIsNone(self.proof(self.inventory(MAIN.replace('62: Grip:=TRUE;', child))))

    def test_exposing_one_of_two_instances_preserves_that_instance(self):
        rows = self.inventory(children=('FixtureA', 'Peer'), caller_extra=' Observed:=Peer.Fine;')
        self.assertIsNotNone(self.proof(rows))
        self.assertIsNone(self.proof(rows, 'Bay.Peer.Fine'))

    def test_final_lists_replay_and_explicit_reference_keep_the_actual_parent_exit(self):
        rows = self.inventory()
        inventory = {'candidates': list(rows.values()), 'inventory_sha256': 'fixture'}
        owner, child = rows['ownerfixturea'], rows['bay.fixturea.fine']
        proof = self.proof(rows)
        reviews = []
        for candidate, step, feedback in [(owner, 17, 'Arrived'), (child, 4, 'Near')]:
            write = next(w['id'] for w in candidate['transitions'] if w['from'] == step)
            timing = {'scope': 'workstation', 'operation': '工件定位', 'boundary_write': write}
            if candidate is child:
                timing.update(scope='production_substep', parent_relation='主步并发推进', parent_link=proof['start_links'][0])
            reviews.append({'name': candidate['name'], 'basis': [write], 'role': 'component_sequence',
                            'reason': '定位反馈控制本实例步进', 'use': 'production',
                            'boundaries': [{'write': write, 'action': '定位', 'via': 'signal:' + feedback}],
                            'timing_basis': timing})
        for request in [(), (child['identity'],)]:
            with self.subTest(explicit=request):
                decisions = {'inventory_sha256': 'fixture', 'decisions': reviews}
                _, assessed = scope_selection(inventory, decisions, required_channels=request)
                primary, optional = capture_lists(inventory, assessed, {})
                actual = next(d for d in assessed if d['name'] == child['name'])
                self.assertEqual(actual['effective_use'], 'reference' if request else 'detail')
                self.assertEqual(actual['admission'], 'retained' if request else 'optional')
                self.assertEqual(child['identity'] in primary, bool(request))
                self.assertEqual([(r[0], r[1]) for r in optional], [] if request else [(child['name'], '内部细节')])
                joins = [r for r in capture_context(primary, assessed) if r[0] == owner['name'] and r[4:6] == (51, 9)]
                self.assertEqual(len(joins), 1)
                self.assertIn(child['name'], joins[0][-1])
                reviews = [dict(d, admission='captured', effective_use='production') for d in assessed]
