import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.setup_migration import can_migrate_pending
from tests.test_capture_coverage import neutral, sysmac


def restore_flat_layout(out, status):
    """Recreate the 18.5.1 flat checkpoint with the same source-derived evidence."""
    prior = dict(status)
    prior.pop('trace_targets_stage')
    prior.update(version='18.5.1', static_variables=0, discovery='step_variable_discovery.json',
                 request_sha256=hashlib.sha256(b'previous pipeline request').hexdigest())
    hashes = {}
    for relative, digest in status['artifact_sha256'].items():
        file = out / relative
        if file.parent != out:
            file.rename(out / file.name)
        hashes[file.name] = digest
    (out / 'evidence').rmdir()
    static = out / 'static_variables.txt'
    preview = out / 'trace_targets_preview.txt'
    static.rename(preview)
    static.write_bytes(b'')
    hashes[preview.name] = hashes[static.name]
    hashes[static.name] = hashlib.sha256(b'').hexdigest()
    prior.update(trace_targets=str(preview), artifact_sha256=hashes)
    if prior.get('scope_recheck'):
        prior['scope_recheck'] = str(out / 'scope_recheck.json')
    (out / 'workflow_status.json').write_text(json.dumps(prior), encoding='utf-8')
    return prior


class SetupMigrationTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        self.source, self.out = root / 'line.xml', root / 'setup'
        self.reference = root / 'commissioned.txt'
        self.reference.write_text('Lift\n', encoding='utf-8')
        logic = ('CASE Lift OF 1: Lift:=4; 4: Lift:=1; END_CASE; '
                 'CASE Drive.Cursor OF 1: Drive.Cursor:=4; 4: Drive.Cursor:=1; END_CASE;')
        self.source.write_text(sysmac([('Lift', 'INT'), ('Drive', 'Motor')], [('Move', logic)]))

    def run_setup(self, **options):
        return generate(self.source, self.out, reference=self.reference, choose_available=True, **options)

    def test_first_pass_adopts_canonical_addresses_and_preserves_user_inputs(self):
        old = restore_flat_layout(self.out, self.run_setup())
        addresses = Path(old['trace_targets']).read_bytes()
        self.assertIn(b'Lift\n', addresses)
        user_file = self.out / 'scope_review_input.json'
        user_file.write_bytes(b'field work in progress')
        new = self.run_setup()
        self.assertEqual(new['out'], str(self.out))
        self.assertEqual(new['trace_targets_stage'], 'preview')
        self.assertEqual((self.out / 'static_variables.txt').read_bytes(), addresses)
        self.assertFalse(Path(old['trace_targets']).exists())
        self.assertTrue((self.out / 'evidence' / old['discovery']).is_file())
        self.assertFalse((self.out / old['discovery']).exists())
        self.assertEqual(user_file.read_bytes(), b'field work in progress')
        self.assertEqual(self.run_setup(), new)
        self.assertFalse(self.out.with_name('setup_2').exists())

    def test_recheck_resumes_with_saved_decisions_and_checks_activation_before_publication(self):
        first = self.run_setup()
        inventory = json.loads((self.out / first['discovery']).read_bytes())
        submitted = self.out.parent / 'scope.json'
        submitted.write_text(json.dumps(neutral(inventory)))
        queue = [{'name': 'Drive.Cursor', 'reason': '工步写入待复核'}]
        with patch('plc_trace.capture.scope_recheck.scope_recheck', return_value=queue):
            pending = restore_flat_layout(self.out, self.run_setup(scope_results=submitted))
            submitted.unlink()
            marker = self.out / 'workflow_status.json'
            original = marker.read_bytes()
            marker.write_text(json.dumps(dict(pending, activation_sha256='wrong')))
            with self.assertRaises(ValueError):
                generate(self.source, self.out, reference=self.reference)
            self.assertTrue(Path(pending['trace_targets']).exists())
            self.assertFalse((self.out / 'evidence').exists())
            marker.write_bytes(original)
            resumed = self.run_setup()
        self.assertEqual(resumed['status'], 'scope_recheck_pending')
        self.assertEqual(resumed['scope_input_sha256'], pending['scope_input_sha256'])
        self.assertEqual(json.loads(Path(resumed['scope_recheck']).read_bytes())['items'], queue)
        self.assertFalse(Path(pending['scope_recheck']).exists())
        self.assertFalse((self.out / 'scope_results.json').exists())
        self.assertTrue((self.out / 'evidence/scope_results.json').is_file())
        self.assertEqual(self.run_setup(), resumed)

    def test_only_compatible_requests_and_unfinished_flat_18_5_1_runs_are_migrated(self):
        old = restore_flat_layout(self.out, self.run_setup(task_name='Run'))
        request = dict(source=self.source, digest=old['source_sha256'], automatic=True, capture_format='auto',
                       task_name='Run', every_n_cycles=None, reference_sha=old['coverage']['reference_sha256'])
        self.assertTrue(can_migrate_pending(old, self.out, **request))
        inovance = old | dict(source_format='plcopen', capture_format='inovance')
        for verified, task, matches in [(True, ' rUN ', True), (False, ' Run ', True), (False, 'rUN', False)]:
            with self.subTest(task_verified=verified, supplied_task=task):
                self.assertEqual(can_migrate_pending(inovance | dict(task_verified_in_xml=verified), self.out,
                                                     **(request | dict(task_name=task))), matches)
        for change in [dict(source=self.reference), dict(digest='different'), dict(automatic=False),
                       dict(capture_format='inovance'), dict(task_name=None), dict(every_n_cycles=5),
                       dict(reference_sha=None)]:
            with self.subTest(change=change):
                self.assertFalse(can_migrate_pending(old, self.out, **(request | change)))
        for change in [dict(version='18.4.0'), dict(status='done_with_review'),
                       dict(out=str(self.out.parent)), dict(trace_targets_stage='preview')]:
            with self.subTest(change=change):
                self.assertFalse(can_migrate_pending(old | change, self.out, **request))
        Path(old['trace_targets']).write_bytes(b'modified')
        with self.assertRaisesRegex(ValueError, '缺失或已修改'):
            self.run_setup(task_name='Run')
        self.assertTrue((self.out / 'trace_targets_preview.txt').is_file())
