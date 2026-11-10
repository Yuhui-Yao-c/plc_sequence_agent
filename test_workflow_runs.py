import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from plc_cli import main
from workflow import create, lock_run_dir


class WorkflowRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root / 'out'
        self.source = self.root / 'PLC.xml'
        self.source.write_text(
            '<project><pou name="Demo"><body><ST><xhtml xmlns="http://www.w3.org/1999/xhtml">'
            'CASE Cell.Phase[4] OF 10: IF Ready THEN Cell.Phase[4]:=20; END_IF; '
            '20: IF Reset THEN Cell.Phase[4]:=10; END_IF; END_CASE;'
            '</xhtml></ST></body></pou></project>', encoding='utf-8')
        self.trace = self.root / '20260101080000_00001.csv'
        self.trace.write_text(
            'TraceType,Continuous\nSampleTimingType,interval\nStartRawTime,1700000000000000000\n'
            'Index,RawTime,Cell.Phase[4]:INT\n0,1700000000000000000,10\n1,1700000000012000000,20\n',
            encoding='utf-8')

    def cli(self, mode, *flags):
        args = ['plc_cli.py', mode, '--source', str(self.source), '--out', str(self.out)]
        if mode == 'trace':
            args.extend(['--trace', str(self.trace)])
        with patch('sys.argv', args + list(flags)), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main()

    def read(self, name):
        return json.loads((self.out / name).read_text(encoding='utf-8'))

    def result_path(self, input_path):
        return input_path.with_name(input_path.name.replace('.input.json', '.result.json'))

    def test_trace_rebuild_archives_and_only_resumes_current_results(self):
        self.cli('trace')
        batch_path, = (self.out / 'batches_desc').glob('*.input.json')
        result_path = self.result_path(batch_path)
        old = json.loads(batch_path.read_text(encoding='utf-8'))
        row, = old['rows']
        result = {'run_id': old['run_id'], 'batch_id': old['batch_id'],
                  'descs': {row['id']: {'ok': False, 'basis': row['basis'], 'reason': '证据不足'}}}
        old_result = json.dumps(result, ensure_ascii=False)
        result_path.write_text(old_result, encoding='utf-8')
        with self.assertRaisesRegex(SystemExit, '空目录'):
            self.cli('trace')
        self.assertEqual(self.read('trace_prepared.json')['run_id'], old['run_id'])
        self.cli('trace', '--force')
        status = self.read('workflow_status.json')
        archive = Path(status['previous_output'])
        self.assertEqual((archive / result_path.relative_to(self.out)).read_text(encoding='utf-8'), old_result)
        self.assertFalse(result_path.exists())
        self.assertEqual(status['b_tier_pending_rows'], 1)
        new = json.loads(batch_path.read_text(encoding='utf-8'))
        self.assertNotEqual(old['run_id'], new['run_id'])
        self.assertNotEqual(row['basis'], new['rows'][0]['basis'])

        result_path.write_text(old_result, encoding='utf-8')
        self.cli('trace', '--resume')
        self.assertEqual(self.read('workflow_status.json')['invalid_b_tier_batches'], [result_path.name])
        result['run_id'] = new['run_id']
        result_path.write_text(json.dumps(result), encoding='utf-8')
        self.cli('trace', '--resume')
        self.assertEqual(self.read('workflow_status.json')['b_tier_invalid_rows'], 1)
        result['descs'][row['id']]['basis'] = new['rows'][0]['basis']
        result_path.write_text(json.dumps(result), encoding='utf-8')
        self.cli('trace', '--resume')
        status = self.read('workflow_status.json')
        self.assertTrue(status['preparation_reused'])
        self.assertEqual(status['run_id'], new['run_id'])
        self.assertEqual(status['b_tier_pending_rows'], 0)
        self.assertEqual(status['unresolved_rows'], 1)

        with patch('plc_cli.pipeline_fingerprint', return_value='changed-version'):
            with self.assertRaisesRegex(SystemExit, '无法续跑'):
                self.cli('trace', '--resume')

    def test_trace_has_one_entrypoint_and_only_trace_outputs(self):
        self.cli('trace')
        self.assertEqual(self.read('workflow_manifest.json')['extraction']['format'], 'plcopen')
        self.assertFalse((self.out / 'batches').exists())
        self.assertFalse((self.out / 'sequence_candidates.csv').exists())
        self.assertFalse((self.out / 'source_transition_graph.json').exists())
        for mode in ('predict', 'finish', 'coverage', 'evaluate'):
            with self.subTest(mode=mode), self.assertRaises(SystemExit) as error:
                self.cli(mode)
            self.assertEqual(error.exception.code, 2)

    def test_trace_reads_smc2_source_without_candidate_preparation(self):
        self.source = self.root / 'PLC.smc2'
        network = {'CMT': '10：等待进料', 'CLs': [{'__type': 'IST', 'Ix': 1, 'X': 0, 'Y': 0,
                    'TXT': 'IF Cell.Phase[4]=10 AND Ready THEN Cell.Phase[4]:=20; END_IF;'}]}
        self.source.write_text(f'<smc2><entry type="json" name="Feed"><![CDATA[{json.dumps(network, ensure_ascii=False)}]]>'
                               '</entry></smc2>', encoding='utf-8')
        self.cli('trace')
        status = self.read('workflow_status.json')
        self.assertEqual((status['source_format'], status['unique_pairs'], status['a_sourced']), ('smc2', 1, 1))
        evidence = self.read('trace_source_evidence.json')['pairs']['Cell.Phase[4]=10|||Cell.Phase[4]=20']
        self.assertEqual((evidence['relation'], evidence['description']), ('direct', '等待进料'))
        self.assertIn('Ready', evidence['direct'][0]['guard'])
        self.assertFalse((self.out / 'batches').exists())

    def test_rebuild_protects_inputs_and_refuses_a_concurrent_writer(self):
        self.out.mkdir()
        (self.out / 'workflow_manifest.json').write_text('{}', encoding='utf-8')
        nested_source = self.out / 'PLC.xml'
        nested_source.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(ValueError, '输入分开'):
            create(nested_source, self.out, trace=self.trace, force=True)
        self.assertTrue(nested_source.is_file())
        self.assertEqual(list(self.root.glob('out.previous-*')), [])
        with lock_run_dir(self.out), self.assertRaisesRegex(SystemExit, '正在由其他工作流命令使用'):
            self.cli('trace', '--force')
