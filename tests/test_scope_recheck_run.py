import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plc_trace.capture.extract_step_variables import generate
from tests.test_capture_coverage import neutral, sysmac


class ScopeRecheckRunTests(unittest.TestCase):
    def test_incomplete_scope_can_be_corrected_in_place_after_a_failed_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'line.xml', root / 'setup'
            review, reference = root / 'review.json', root / 'commissioned.txt'
            reference.write_text('Lift\n')
            names = ['Lift', 'Drive.Cursor']
            source.write_text(sysmac([('Lift', 'INT'), ('Drive', 'Motor')],
                [('Cell', ' '.join(f'CASE {n} OF 1: IF Ready THEN {n}:=4; END_IF; 4: {n}:=1; END_CASE;'
                                   for n in names))]))

            def run(**options):
                return generate(source, output, reference=reference, choose_available=True, **options)

            first = run()
            self.assertEqual(first['status'], 'scope_review_pending')
            inventory = json.loads((output / 'evidence/step_variable_discovery.json').read_bytes())
            decisions = neutral(inventory)
            review.write_text(json.dumps(decisions))

            def countercheck(_inventory, roles, required_keys):
                self.assertEqual(set(required_keys), {'lift'})
                row = next(r for r in roles if r['name'] == 'Drive.Cursor')
                return [] if row['admission'] == 'captured' else [
                    {'name': row['name'], 'reason': '受控动作待核', 'write': 't1'}]

            with patch('plc_trace.capture.scope_recheck.scope_recheck', side_effect=countercheck):
                foreign = output / 'evidence/scope_recheck.json'
                foreign.write_text('现场补充说明')
                with self.assertRaisesRegex(ValueError, '已有其他文件'):
                    run(scope_results=review)
                self.assertEqual(foreign.read_text(), '现场补充说明')
                self.assertEqual(run(), first)
                foreign.unlink()
                pending = run(scope_results=review)
                self.assertEqual(pending['status'], 'scope_recheck_pending')
                self.assertEqual(pending['next_action'], 'recheck_scope')
                self.assertIsNone(pending['member_type_template'])
                self.assertFalse((output / 'missing_member_types.csv').exists())
                self.assertEqual((pending['scope_recheck_count'], pending['proposed_target_count']), (1, 1))
                self.assertFalse(pending['configuration_complete'])
                self.assertFalse(pending['coverage_complete'])
                self.assertIsNone(pending['configuration'])
                self.assertEqual(pending['configured_variables'], 0)
                self.assertEqual(pending['static_variables'], len(names))
                self.assertEqual(pending['trace_targets_stage'], 'preview')
                self.assertEqual(Path(pending['trace_targets']), output / 'static_variables.txt')
                self.assertCountEqual(Path(pending['trace_targets']).read_text().splitlines(), names)
                self.assertEqual(pending['preview_configured_variables'], 1)
                self.assertEqual(list(pending['preview_unresolved_types']), ['Drive.Cursor'])
                queue = Path(pending['scope_recheck'])
                self.assertEqual(json.loads(queue.read_bytes())['items'][0]['name'], 'Drive.Cursor')
                self.assertEqual(run(scope_results=review), pending)
                # A failed correction must preserve both the task and the usable draft.
                review.write_text(json.dumps(dict(decisions, inventory_sha256='wrong')))
                with self.assertRaisesRegex(ValueError, '指纹'):
                    run(scope_results=review)
                self.assertEqual(run(), pending)

                row = next(d for d in decisions['decisions'] if d['name'] == 'Drive.Cursor')
                row.update(role='component_sequence', use='production',
                           timing_basis={'scope': 'workstation', 'operation': '送料', 'boundary_write': 't1'},
                           boundaries=[{'write': 't1', 'via': 'signal:Ready', 'action': '送料'}])
                review.write_text(json.dumps(decisions))
                final = run(scope_results=review)
                self.assertEqual(final['status'], 'needs_review')  # Motor.Cursor still has no leaf type.
                self.assertEqual((final['static_variables'], final['configured_variables']), (2, 1))
                self.assertEqual(final['out'], first['out'])
                self.assertFalse(queue.exists())
                self.assertEqual(pending['trace_targets'], final['trace_targets'])
                self.assertEqual(final['trace_targets_stage'], 'final')
                self.assertFalse(Path(pending['preview_configuration']).exists())
                self.assertEqual(run(scope_results=review), final)
                self.assertEqual(run(scope_results=output / 'evidence/scope_results.json'), final)
                self.assertFalse((root / 'setup_2').exists())
