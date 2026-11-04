"""Execution integrity and resumability tests; semantic results are synthetic."""
import contextlib
import csv
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from action_describe import cmd_a, cmd_build
from workflow import create, validate
from workflow_control import (CONTRACT, COMPLETION, audit_run, complete_run,
                              digest, read_json, seal_run, write_json)
from test_v9_7 import st


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.xml'
        self.source.write_text(st('CASE Cell.Phase OF 10: Cell.Phase:=20; 20: Cell.Phase:=30; 30: Cell.Phase:=10; END_CASE;'))
        self.run = self.root / 'run'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            create(self.source, self.run)
            cmd_a(str(self.run))
            cmd_build(str(self.run))
        self.contract = seal_run(self.run)

    def emit(self, kind=None, bound=True):
        for batch in self.contract['batches']:
            if kind and batch['kind'] != kind:
                continue
            payload = read_json(self.run / batch['input'])
            if batch['kind'] == 'process':
                steps = [{'开始标签': c['开始标签'], '结束标签': c['结束标签'],
                          '构成要素': c['构成要素'], '动作': '测试等待', '动作描述': '测试等待',
                          '证据候选ID': [c['候选ID']], '证据事件ID': [],
                          '过程类型': '工艺等待', '统计对象': '测试对象',
                          '起点含义': '进入状态', '终点含义': '离开状态',
                          '工艺依据': 'synthetic source fixture', '边界依据': 'synthetic source transition'}
                         for c in payload['candidates']]
                data = {'batch_id': batch['batch_id'], 'steps': steps,
                        'unresolved_events': [{'event_id': e['事件ID'], 'reason': '测试中保留未解决事件'}
                                              for e in payload['events']]}
            else:
                data = {'batch_id': batch['batch_id'], 'descs': {
                    r['id']: {'ok': False, 'reason': '合成测试没有工艺上下文，保留复核'} for r in payload['rows']}}
            if bound:
                data['input_sha256'] = batch['input_sha256']
            write_json(self.run / batch['output'], data)

    def result_path(self, kind):
        return self.run / next(b for b in self.contract['batches'] if b['kind'] == kind)['output']

    def tree_bytes(self):
        return {p.relative_to(self.run).as_posix(): p.read_bytes() for p in self.run.rglob('*') if p.is_file()}

    def test_sealing_preserves_engine_artifact_bytes(self):
        before = self.tree_bytes()
        seal_run(self.run)
        self.assertEqual(before, self.tree_bytes())

    def test_status_is_read_only_even_without_step_comments(self):
        self.assertFalse((self.run / 'step_comments.json').exists())
        before = self.tree_bytes()
        status = audit_run(self.run)
        self.assertEqual(status['state'], 'WAITING_FOR_BATCHES')
        self.assertEqual(before, self.tree_bytes())

    def test_both_batch_families_are_reported(self):
        status = audit_run(self.run)
        self.assertGreater(status['batches']['process']['pending'], 0)
        self.assertGreater(status['batches']['description']['pending'], 0)
        self.assertEqual({x['kind'] for x in status['next_work']}, {'process', 'description'})

    def test_process_completion_does_not_hide_pending_descriptions(self):
        self.emit('process')
        sentinel = self.run / 'final_prediction_pairs.csv'
        sentinel.write_bytes(b'previous deliverable')
        result = complete_run(self.run)
        self.assertFalse(result['completed'])
        self.assertEqual(result['batches']['description']['pending'], 1)
        self.assertEqual(sentinel.read_bytes(), b'previous deliverable')

    def test_malformed_json_is_reported(self):
        self.emit()
        self.result_path('description').write_text('{broken')
        self.assertTrue(audit_run(self.run)['errors'])

    def test_wrong_input_receipt_is_rejected(self):
        self.emit()
        path = self.result_path('process')
        result = read_json(path); result['input_sha256'] = '0' * 64
        write_json(path, result)
        self.assertFalse(audit_run(self.run)['ready_to_finish'])

    def test_old_result_cannot_be_silently_reused_in_new_run(self):
        self.emit(bound=False)
        self.assertTrue(any('未绑定' in e for e in audit_run(self.run)['errors']))

    def test_changed_batch_input_is_rejected(self):
        self.emit()
        path = self.run / self.contract['batches'][0]['input']
        path.write_bytes(path.read_bytes() + b'\n')
        self.assertTrue(any('输入已变化' in e for e in audit_run(self.run)['errors']))

    def test_changed_candidate_pool_is_rejected(self):
        self.emit()
        path = self.run / 'prediction_candidate_pairs.csv'
        path.write_bytes(path.read_bytes() + b'\n')
        self.assertTrue(any('证据已变化' in e for e in audit_run(self.run)['errors']))

    def test_changed_source_is_rejected(self):
        self.source.write_bytes(self.source.read_bytes() + b'\n')
        self.assertTrue(any('源码文件' in e for e in audit_run(self.run)['errors']))

    def test_changed_trace_is_rejected(self):
        trace = self.root / 'input.trace'; trace.write_bytes(b'original trace')
        manifest = read_json(self.run / 'workflow_manifest.json'); manifest['trace'] = str(trace)
        write_json(self.run / 'workflow_manifest.json', manifest); seal_run(self.run)
        trace.write_bytes(b'changed trace')
        self.assertTrue(any('Trace文件' in e for e in audit_run(self.run)['errors']))

    def test_manual_file_is_not_read_by_prediction_or_status(self):
        manual = self.root / 'manual.xlsx'; manual.write_bytes(b'private manual fixture')
        manifest = read_json(self.run / 'workflow_manifest.json'); manifest['manual'] = str(manual)
        write_json(self.run / 'workflow_manifest.json', manifest)
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path.resolve() == manual.resolve():
                raise AssertionError('manual contents were read')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            seal_run(self.run)
            status = audit_run(self.run)
        self.assertFalse(status['errors'])
        self.assertIn('plc-coverage-evaluation', status['required_skills'])

    def test_missing_requested_manual_is_reported(self):
        manifest = read_json(self.run / 'workflow_manifest.json'); manifest['manual'] = str(self.root / 'missing.xls')
        write_json(self.run / 'workflow_manifest.json', manifest); seal_run(self.run)
        self.assertTrue(any('manual' in e for e in audit_run(self.run)['errors']))

    def test_description_rows_cannot_be_omitted(self):
        self.emit()
        path = self.result_path('description'); result = read_json(path)
        result['descs'].pop(next(iter(result['descs'])))
        write_json(path, result)
        self.assertTrue(any('不能漏行' in e for e in audit_run(self.run)['errors']))

    def test_description_rows_cannot_be_taken_from_another_batch(self):
        self.emit()
        path = self.result_path('description'); result = read_json(path)
        result['descs']['foreign-row'] = {'ok': False, 'reason': 'foreign'}
        write_json(path, result)
        self.assertTrue(audit_run(self.run)['errors'])

    def test_unresolved_description_requires_explanation(self):
        self.emit()
        path = self.result_path('description'); result = read_json(path)
        next(iter(result['descs'].values())).pop('reason')
        write_json(path, result)
        self.assertTrue(any('必须给出原因' in e for e in audit_run(self.run)['errors']))

    def test_description_references_stay_with_their_row(self):
        self.emit()
        path = self.result_path('description'); result = read_json(path)
        result['descs'][next(iter(result['descs']))] = {'ok': True, 'desc': '等待', 'refs': ['Invented.Command']}
        write_json(path, result)
        self.assertTrue(any('引用超出' in e for e in audit_run(self.run)['errors']))

    def test_old_extra_result_file_is_not_merged(self):
        self.emit()
        write_json(self.run / 'batches_desc' / 'obsolete.result.json', {'descs': {}})
        self.assertTrue(any('旧结果' in e for e in audit_run(self.run)['errors']))

    def test_wrong_description_batch_id_is_rejected(self):
        self.emit()
        path = self.result_path('description'); result = read_json(path); result['batch_id'] = 999
        write_json(path, result)
        self.assertTrue(any('批次ID' in e for e in audit_run(self.run)['errors']))

    def test_legacy_results_remain_supported_but_are_labelled_unsealed(self):
        (self.run / CONTRACT).unlink()
        self.emit(bound=False)
        path = self.result_path('description'); result = read_json(path); result.pop('batch_id')
        write_json(path, result)
        status = audit_run(self.run)
        self.assertTrue(status['ready_to_finish'], status['errors'])
        self.assertEqual(status['input_binding'], 'legacy_unsealed')

    def test_complete_produces_auditable_deliverables_with_review(self):
        self.emit()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = complete_run(self.run)
        self.assertTrue(result['completed'], result['errors'])
        self.assertEqual(result['state'], 'COMPLETED_WITH_REVIEW')
        self.assertTrue((self.run / COMPLETION).is_file())
        self.assertEqual(audit_run(self.run)['state'], 'COMPLETED_WITH_REVIEW')
        self.assertTrue((self.run / 'final_prediction_pairs_full.csv').is_file())

    def test_completion_is_idempotent(self):
        self.emit()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(complete_run(self.run)['completed'])
        before = self.tree_bytes()
        original_validate = validate
        def guarded(*args, **kwargs):
            self.assertFalse(kwargs.get('write', False))
            return original_validate(*args, **kwargs)
        with patch('workflow.validate', guarded):
            self.assertTrue(complete_run(self.run)['completed'])
        self.assertEqual(before, self.tree_bytes())

    def test_workbook_failure_is_not_complete_but_csv_is_preserved(self):
        self.emit()
        with patch('gen_workstation_xlsx.generate', side_effect=RuntimeError('test workbook failure')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = complete_run(self.run)
        self.assertTrue(result['core_success'])
        self.assertFalse(result['completed'])
        self.assertEqual(result['state'], 'DELIVERY_INCOMPLETE')
        self.assertTrue((self.run / 'final_prediction_pairs.csv').is_file())
        status = audit_run(self.run)
        self.assertEqual(status['state'], 'DELIVERY_INCOMPLETE')
        self.assertTrue(status['delivery_errors'])

    def test_failed_delivery_can_be_retried_after_the_problem_is_fixed(self):
        self.emit()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with patch('gen_workstation_xlsx.generate', side_effect=RuntimeError('test temporary failure')):
                self.assertFalse(complete_run(self.run)['completed'])
            self.assertTrue(complete_run(self.run)['completed'])

    def test_missing_artifact_invalidates_completed_receipt(self):
        self.emit()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(complete_run(self.run)['completed'])
        (self.run / 'sequence_atomic_conditions.json').unlink()
        self.assertFalse(audit_run(self.run)['completed'])

    def test_trace_failure_is_not_complete(self):
        trace = self.root / 'input.trace'; trace.write_bytes(b'fixture')
        manifest = read_json(self.run / 'workflow_manifest.json'); manifest['trace'] = str(trace)
        write_json(self.run / 'workflow_manifest.json', manifest)
        self.contract = seal_run(self.run); self.emit()
        with patch('trace_jump_analyzer.analyze', side_effect=RuntimeError('test trace failure')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = complete_run(self.run)
        self.assertFalse(result['completed'])
        self.assertTrue(any('trace_analysis' in e for e in result['errors']))

    def test_missing_trace_outputs_are_not_reported_as_complete(self):
        trace = self.root / 'input.trace'; trace.write_bytes(b'fixture')
        manifest = read_json(self.run / 'workflow_manifest.json'); manifest['trace'] = str(trace)
        write_json(self.run / 'workflow_manifest.json', manifest)
        self.contract = seal_run(self.run); self.emit()
        with patch('trace_jump_analyzer.analyze', return_value={'matched_pairs': 0, 'prediction_pairs': 0}):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = complete_run(self.run)
        self.assertFalse(result['completed'])
        self.assertTrue(any('Trace分析缺少' in e for e in result['errors']))

    def test_coverage_failure_is_not_complete(self):
        manual = self.root / 'manual.xls'; manual.write_bytes(b'fixture')
        manifest = read_json(self.run / 'workflow_manifest.json'); manifest['manual'] = str(manual)
        write_json(self.run / 'workflow_manifest.json', manifest)
        self.contract = seal_run(self.run); self.emit()
        with patch.dict(sys.modules, {'gen_coverage_eval': type('BrokenCoverage', (), {
                'infer_proc_name': staticmethod(lambda p: 'fixture'),
                'infer_kind': staticmethod(lambda p, k: 'codesys'),
                'main_with': staticmethod(lambda *a: (_ for _ in ()).throw(RuntimeError('test coverage failure'))),
        })}):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = complete_run(self.run)
        self.assertFalse(result['completed'])
        self.assertTrue(any('coverage' in e for e in result['errors']))

    def test_unnecessary_b_tier_is_not_required(self):
        path = self.run / 'action_desc_a.csv'
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream); fields = reader.fieldnames; rows = list(reader)
        for row in rows:
            row['A档描述'] = '等待许可'
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        for path in (self.run / 'batches_desc').glob('*.input.json'):
            path.unlink()
        self.contract = seal_run(self.run); self.emit()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = complete_run(self.run)
        self.assertTrue(result['completed'], result['errors'])
        self.assertEqual(result['batches']['description']['total'], 0)

    def test_legacy_finish_and_complete_keep_identical_csv_and_xlsx_cells(self):
        self.emit()
        reference = self.root / 'reference'
        shutil.copytree(self.run, reference)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(validate(reference, write=True)['success'])
            self.assertTrue(complete_run(self.run)['completed'])
        for path in reference.glob('*.csv'):
            self.assertEqual(path.read_bytes(), (self.run / path.name).read_bytes(), path.name)
        for suffix in ('atomic_conditions.json', 'atomic_summary.json', 'collection_summary.json'):
            for path in reference.glob('*_' + suffix):
                self.assertEqual(read_json(path), read_json(self.run / path.name), path.name)
        from openpyxl import load_workbook
        first = load_workbook(next(reference.glob('*.xlsx')))
        second = load_workbook(next(self.run.glob('*.xlsx')))
        self.assertEqual(list(first.active.values)[2:], list(second.active.values)[2:])
        self.assertEqual(str(first.active.merged_cells), str(second.active.merged_cells))
        first.close(); second.close()

    def test_changed_result_invalidates_completion_receipt(self):
        self.emit()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(complete_run(self.run)['completed'])
        path = self.result_path('description'); result = read_json(path)
        next(iter(result['descs'].values()))['reason'] = 'updated explicit review reason'
        write_json(path, result)
        status = audit_run(self.run)
        self.assertFalse(status['completed']); self.assertTrue(status['ready_to_finish'])

    def test_results_changed_during_assembly_do_not_produce_a_completed_receipt(self):
        self.emit()
        original_validate = validate
        def changed(*args, **kwargs):
            result = original_validate(*args, **kwargs)
            if kwargs.get('write'):
                path = self.result_path('description'); data = read_json(path)
                next(iter(data['descs'].values()))['reason'] = 'changed during assembly'
                write_json(path, data)
            return result
        with patch('workflow.validate', changed):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = complete_run(self.run)
        self.assertFalse(result['completed'])
        self.assertTrue(any('组装期间变化' in error for error in result['errors']))

    def test_new_protocol_does_not_require_fabricated_descriptions(self):
        self.emit()
        status = audit_run(self.run)
        self.assertTrue(status['ready_to_finish'], status['errors'])
        self.assertEqual(status['batches']['description']['pending'], 0)

    def test_status_cannot_modify_or_escape_to_another_run(self):
        manifest = read_json(self.run / 'workflow_manifest.json')
        manifest['batches'][0]['input'] = '../source.xml'
        write_json(self.run / 'workflow_manifest.json', manifest)
        self.assertTrue(audit_run(self.run)['errors'])


class DeclarationTests(unittest.TestCase):
    def test_all_seven_project_skills_are_discoverable_and_allowed(self):
        root = Path(__file__).parent
        config = read_json(root / 'opencode.json')
        skills = list((root / '.opencode' / 'skills').glob('*/SKILL.md'))
        self.assertEqual(len(skills), 7)
        for path in skills:
            text = path.read_text(encoding='utf-8')
            self.assertTrue(text.startswith('---\n'), str(path))
            header = text.split('---', 2)[1]
            self.assertIn('name: ' + path.parent.name, header)
            self.assertIn('description:', header)
            self.assertEqual(config['permission']['skill'].get(path.parent.name), 'allow')


if __name__ == '__main__':
    unittest.main()
