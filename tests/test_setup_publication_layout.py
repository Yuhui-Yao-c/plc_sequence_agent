import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plc_trace.capture.setup_output import publish, select_run_dir, verify_files
from plc_trace.runtime.workflow import fingerprint


class SetupPublicationLayoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out = Path(tmp.name) / 'line'
        self.out.mkdir()

    def test_flat_migration_rolls_back_without_touching_user_input(self):
        old = {'static_variables.txt': b'', 'trace_targets_preview.txt': b'Bay.Cursor\n',
               'scope_candidates.jsonl': b'old', 'line_trace_config_preview.csv': b'vendor'}
        for key, data in old.items():
            (self.out / key).write_bytes(data)
        status = {'status': 'scope_review_pending', 'out': str(self.out),
                  'trace_targets': str(self.out / 'trace_targets_preview.txt'),
                  'preview_configuration': str(self.out / 'line_trace_config_preview.csv'),
                  'artifact_sha256': {key: fingerprint(self.out / key) for key in old}}
        marker = self.out / 'workflow_status.json'
        marker.write_text(json.dumps(status))
        user_file = self.out / 'scope_review_input.json'
        user_file.write_bytes(b'user')
        new = {'workflow_status.json': b'new status', 'static_variables.txt': old['trace_targets_preview.txt'],
               'evidence/scope_candidates.jsonl': b'new evidence', 'line_trace_config.csv': b'vendor'}

        def snapshot():
            return {p.relative_to(self.out): p.read_bytes() if p.is_file() else None
                    for p in self.out.rglob('*')}

        before = snapshot()
        replace = Path.replace

        def reject_marker(path, dest):
            if path.parent.name == 'ready' and dest == marker:
                raise PermissionError('locked')
            return replace(path, dest)

        with patch.object(Path, 'replace', reject_marker), self.assertRaisesRegex(PermissionError, 'locked'):
            publish(self.out, new, force=False, completing=True)
        self.assertEqual(snapshot(), before)
        publish(self.out, new, force=False, completing=True)
        self.assertEqual(set(snapshot()), {Path(key) for key in new} | {Path('evidence'), Path(user_file.name)})
        self.assertEqual(user_file.read_bytes(), b'user')

    def test_verify_does_not_trust_paths_in_flat_or_nested_manifests(self):
        listing = self.out / 'static_variables.txt'
        listing.write_bytes(b'x')
        for stage in (None, 'preview'):
            status = {'status': 'scope_review_pending', 'trace_targets': str(listing)}
            if stage:
                status['trace_targets_stage'] = stage
            for bad in ('../note', r'evidence\..\note', 'evidence/../static_variables.txt', 'evidence/note.txt',
                        'foreign_trace_config.csv', 'workflow_status.json', 'custom.txt'):
                with self.subTest(stage=stage, bad=bad):
                    status['artifact_sha256'] = {listing.name: fingerprint(listing), bad: 'unused'}
                    with self.assertRaises(ValueError):
                        verify_files(self.out, status)

    def test_draft_cannot_be_reused_as_a_confirmed_list(self):
        listing = self.out / 'static_variables.txt'
        listing.write_bytes(b'Bay.Cursor\n')
        status = {'status': 'scope_review_pending', 'trace_targets': str(listing),
                  'trace_targets_stage': 'final', 'artifact_sha256': {listing.name: fingerprint(listing)}}
        with self.assertRaisesRegex(ValueError, '阶段'):
            verify_files(self.out, status)

    def test_evidence_directory_must_not_redirect_a_generated_write(self):
        alias = self.out / 'evidence'
        external = self.out.parent / 'private'
        external.mkdir()
        try:
            alias.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaisesRegex(ValueError, '证据目录'):
            publish(self.out, {'evidence/scope_results.json': b'{}'}, force=False, completing=True)
        self.assertEqual(list(external.iterdir()), [])

    def test_foreign_file_with_identical_bytes_is_never_adopted(self):
        evidence = self.out / 'evidence'
        evidence.mkdir()
        path = evidence / 'scope_results.json'
        path.write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, '已有其他文件'):
            publish(self.out, {'evidence/scope_results.json': b'{}'}, force=False, completing=True)
        self.assertEqual(path.read_bytes(), b'{}')

    def test_matching_current_run_precedes_migratable_pending_runs(self):
        for index in (1, 3, 2):
            path = self.out if index == 1 else self.out.with_name('line_' + str(index))
            path.mkdir(exist_ok=True)
            status = {'mode': 'trace_setup', 'out': str(path), 'status': 'scope_review_pending',
                      'request_sha256': 'new' if index == 3 else 'old'}
            (path / 'workflow_status.json').write_text(json.dumps(status))

        def compatible(status, _path):
            return status.get('request_sha256') == 'old'

        self.assertEqual(select_run_dir(self.out, 'new', compatible=compatible), self.out.with_name('line_3'))
        self.assertEqual(select_run_dir(self.out, 'unseen', compatible=compatible), self.out)
