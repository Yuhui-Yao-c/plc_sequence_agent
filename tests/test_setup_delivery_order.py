import csv
import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.extract_step_variables import generate
from tests.test_capture_coverage import neutral, sysmac


class SetupDeliveryOrderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'Line.xml'
        self.out = self.root / 'setup'

    def write_source(self, declarations, cursors):
        body = ' '.join(f'CASE {n} OF 1: IF Ready THEN {n}:=4; END_IF; 4: {n}:=1; END_CASE;'
                        for n in cursors)
        self.source.write_text(sysmac(declarations, [('Cell', body)]))

    def review(self, production_names):
        inventory = json.loads((self.out / 'step_variable_discovery.json').read_bytes())
        review = neutral(inventory)
        for decision in review['decisions']:
            if decision['name'] in production_names:
                decision.update(role='component_sequence', use='production',
                                boundaries=[{'action': '移送到位', 'write': 't1', 'via': 'signal:Ready'}],
                                timing_basis={'scope': 'workstation', 'operation': '工件移送',
                                              'boundary_write': 't1'})
        path = self.root / 'review.json'
        path.write_text(json.dumps(review))
        return generate(self.source, self.out, scope_results=path)

    def test_only_requested_production_addresses_need_types_after_scope_review(self):
        self.write_source([('Feed', 'INT'), ('Arm', 'ExternalJob'), ('Monitor', 'ExternalPort')],
                          ['Feed', 'Arm.Phase', 'Monitor.Stage'])
        first = generate(self.source, self.out)
        self.assertEqual(first['next_action'], 'review_scope')
        self.assertIsNone(first['member_type_template'])
        self.assertFalse((self.out / 'missing_member_types.csv').exists())
        self.assertEqual(set(first['preview_unresolved_types']), {'Arm.Phase', 'Monitor.Stage'})
        self.assertEqual(first['preview_configured_variables'], 1)

        final = self.review({'Feed', 'Arm.Phase'})
        self.assertEqual(final['next_action'], 'deliver_results')
        self.assertEqual((final['trace_target_count'], final['configured_variables']), (2, 1))
        self.assertEqual(Path(final['trace_targets']).name, 'static_variables.txt')
        self.assertEqual(set(Path(final['trace_targets']).read_text().splitlines()), {'Arm.Phase', 'Feed'})
        self.assertEqual(set(final['unresolved_types']), {'Arm.Phase'})
        with Path(final['member_type_template']).open(encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r['结构类型'], r['成员'], r['地址数']) for r in rows], [('ExternalJob', 'Phase', '1')])
        selection = (self.out / 'static_variables.txt').read_bytes()
        review = (self.out / 'scope_results.json').read_bytes()

        supplement = self.root / 'member_types.csv'
        supplement.write_text('结构类型,成员,数据类型\nExternalJob,Phase,UINT\n')
        complete = generate(self.source, self.out, member_types=supplement)
        self.assertEqual(complete['next_action'], 'deliver_results')
        self.assertTrue(complete['configuration_complete'])
        self.assertEqual(complete['configured_variables'], 2)
        self.assertEqual((self.out / 'static_variables.txt').read_bytes(), selection)
        self.assertEqual((self.out / 'scope_results.json').read_bytes(), review)
        self.assertFalse(Path(final['configuration']).exists())
        self.assertFalse((self.out / 'missing_member_types.csv').exists())

    def test_zero_exportable_channels_still_delivers_the_reviewed_address_list(self):
        self.write_source([('Arm', 'LibraryDrive')], ['Arm.Cursor'])
        first = generate(self.source, self.out)
        self.assertIsNone(first['preview_configuration'])
        self.assertEqual(first['next_action'], 'review_scope')
        final = self.review({'Arm.Cursor'})
        self.assertEqual(final['next_action'], 'deliver_results')
        self.assertIsNone(final['configuration'])
        self.assertEqual((final['static_variables'], final['configured_variables']), (1, 0))
        self.assertEqual(Path(final['trace_targets']).read_text().splitlines(), ['Arm.Cursor'])
        self.assertTrue(Path(final['member_type_template']).is_file())
        with (self.out / 'capture_context.csv').open(encoding='utf-8-sig') as f:
            self.assertEqual([r['步序变量'] for r in csv.DictReader(f)], ['Arm.Cursor'])
