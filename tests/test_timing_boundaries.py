import csv
import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.capture_use import timing_use
from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.step_discovery import Candidate


class TimingBoundaryTests(unittest.TestCase):
    def cursor(self, name, *, with_parent=True):
        candidate = Candidate(name)
        candidate.edge(13, 37, {'scope': 'Seal', 'case_selector': name, 'guard': 'Pressed'})
        if with_parent:
            candidate.links[1] = {'scope': 'Load', 'parent': 'Work', 'parent_step': 170,
                                  'child': name, 'child_value': 37, 'kind': 'waits_for_child'}
        return candidate.record()

    def claim(self):
        return {'use': 'production', 'boundaries': [{'write': 't1', 'action': '等待极耳压合', 'via': 'signal:Pressed'}],
                'timing_basis': {'scope': 'production_substep', 'operation': '极耳压合', 'boundary_write': 't1',
                                 'parent_relation': '上层记录整件装配，本步单独记录极耳压合到位', 'parent_link': 'l1'}}

    def test_source_bound_production_child_survives_renaming_and_private_qualification(self):
        for name in ('设备甲.本地实例.阶段', 'Rack.Unit.Cursor'):
            with self.subTest(name=name):
                accepted = timing_use(self.cursor(name), self.claim(), True)
                self.assertEqual(accepted['admission'], 'captured')
                self.assertEqual(accepted, timing_use(self.cursor(name), accepted, True))

    def test_caller_read_alone_cannot_admit_the_child(self):
        claim = self.claim()
        claim['boundaries'][0]['via'] = 'l1'
        with self.assertRaisesRegex(ValueError, '放行条件'):
            timing_use(self.cursor('Device.Stage'), claim, True)

    def test_parent_comparison_must_reference_a_real_caller_of_this_instance(self):
        row = self.cursor('Device.Stage')
        row['control_links'].append({'id': 'l2', 'parent': 'Device.Stage', 'child': 'Jaw',
                                     'parent_step': 13, 'child_value': 3, 'kind': 'starts_child'})
        for change in ({'parent_link': 'l2'}, {'parent_link': 'l8'}, {'boundary_write': 't2'},
                       {'scope': 'workstation', 'parent_relation': ' '}, {'operation': None}, {'parent_lnik': 'l1'}):
            claim = self.claim()
            claim['timing_basis'].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                timing_use(row, claim, True)

    def test_local_cursor_without_parent_link_can_own_a_production_step(self):
        row = self.cursor('Assembly.内部', with_parent=False)
        claim = self.claim()
        del claim['timing_basis']['parent_link']
        self.assertEqual(timing_use(row, claim, True)['admission'], 'captured')

    def test_legacy_evidence_stays_reviewable_and_replayable(self):
        row, claim = self.cursor('Indexer'), self.claim()
        del claim['timing_basis']
        review = timing_use(row, claim, True)
        self.assertEqual((review['admission'], review['use']), ('review', 'unresolved'))
        self.assertEqual(review['legacy_boundaries'], claim['boundaries'])
        self.assertEqual(timing_use(row, review, True), review)
        review['legacy_boundaries'][0]['via'] = 'signal:Wrong'
        with self.assertRaisesRegex(ValueError, '佐证'):
            timing_use(row, review, True)

    def test_mixed_review_keeps_the_legacy_cursor_in_the_audit_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            xml = root / 'line.xml'
            addresses = ('Press', 'SensorExchange')
            declarations = ''.join(f'<Variable name="{a}"><Type><TypeName>INT</TypeName></Type></Variable>'
                                   for a in addresses)
            jobs = ''.join(f'CASE {a} OF 13: IF Pressed THEN {a}:=37; END_IF; 37: {a}:=13; END_CASE; '
                           for a in addresses)
            xml.write_text(f'<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>{declarations}</GlobalVars>'
                           f'<Program name="P"><BodyContent><ST>{jobs}</ST></BodyContent></Program></Project>')
            out = root / 'run'
            start = generate(xml, out)
            claims = []
            for address in addresses:
                claim = self.claim()
                claim.update(name=address, role='component_sequence', basis=['t1'], reason='按反馈推进')
                claims.append(claim)
            claims[0]['timing_basis'] = {'scope': 'workstation', 'operation': '极耳压合', 'boundary_write': 't1'}
            del claims[1]['timing_basis']
            plan = root / 'review.json'
            plan.write_text(json.dumps({'inventory_sha256': start['inventory_sha256'], 'decisions': claims}))
            result = generate(xml, out, scope_results=plan)
            self.assertEqual([r['name'] for r in result['channels']], ['Press'])
            self.assertFalse(result['coverage_complete'])
            with (out / 'optional_variables.csv').open(encoding='utf-8-sig', newline='') as stream:
                options = list(csv.DictReader(stream))
            self.assertEqual([r['步序变量'] for r in options], ['SensorExchange'])
            self.assertIn('独立计时依据', options[0]['原因'])
            reviewed = out / 'scope_results.json'
            published = json.loads(reviewed.read_bytes())
            unresolved = next(d for d in published['decisions'] if d['name'] == 'SensorExchange')
            self.assertEqual(unresolved['legacy_boundaries'], claims[1]['boundaries'])
            replay = generate(xml, root / 'replay', scope_results=reviewed)
            self.assertEqual(replay['channels'], result['channels'])
