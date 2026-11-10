import csv
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

    def test_native_comparison_only_channels_keep_their_source_station(self):
        from openpyxl import load_workbook

        self.source.write_text('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10">'
                               '<Program name="EM01_上料"><BodyContent><ST>'
                               'IF State[1]=10 THEN Ready:=TRUE; END_IF; '
                               'IF Machine.Phase[1]=10 THEN Done:=TRUE; END_IF;'
                               '</ST></BodyContent></Program>'
                               '<Program name="EM02_下料"><BodyContent><ST>'
                               'IF State[2]=10 THEN Ready:=TRUE; END_IF; '
                               'IF Machine.Phase[2]=10 THEN Done:=TRUE; END_IF;'
                               '</ST></BodyContent></Program></Project>', encoding='utf-8')
        self.trace.write_text('TraceType,Continuous\nSampleTimingType,interval\n'
                              'StartRawTime,1700000000000000000\n'
                              'Index,RawTime,State[1]:INT,State[2]:INT,Machine.Phase[1]:INT,Machine.Phase[2]:INT\n'
                              '0,1700000000000000000,10,10,10,10\n1,1700000000012000000,20,20,20,20\n',
                              encoding='utf-8')
        self.cli('trace')
        with (self.out / 'trace_collection_pairs.csv').open(encoding='utf-8-sig', newline='') as stream:
            pairs = list(csv.DictReader(stream))
        self.assertEqual({r['控制器']: r['构成要素'] for r in pairs}, {
            'State[1]': 'EM01_上料', 'Machine.Phase[1]': 'EM01_上料',
            'State[2]': 'EM02_下料', 'Machine.Phase[2]': 'EM02_下料',
        })
        self.assertEqual({r['源码关系'] for r in pairs}, {'unresolved'})
        status = self.read('workflow_status.json')
        self.assertEqual(status['workstation_xlsx']['workstation_groups'], 2)
        with Path(status['workstation_xlsx']['out']).open('rb') as stream:
            book = load_workbook(stream, read_only=True)
            self.assertEqual({r[1] for r in book.active.iter_rows(min_row=4, values_only=True) if r[6]}, {
                'EM01_上料\nMachine.Phase[1]', 'EM01_上料\nState[1]',
                'EM02_下料\nMachine.Phase[2]', 'EM02_下料\nState[2]',
            })
            book.close()

    def test_smc2_uses_annotated_comparison_when_earlier_comparison_has_no_comment(self):
        self.source = self.root / 'PLC.smc2'
        networks = []
        for value, comment in ((10, ''), (10, '等待夹具到位'), (20, '启动定位'), (20, '等待定位完成')):
            networks.append(json.dumps({'CMT': comment, 'CLs': [{'__type': 'F', 'Name': 'EQ', 'In': [
                {'__type': 'PRM', 'Arg': 'In1', 'Var': 'Cell.Phase[4]'},
                {'__type': 'PRM', 'Arg': 'In2', 'Var': str(value)},
            ]}]}, ensure_ascii=False))
        self.source.write_text('<smc2><entry type="json" name="Fixture"><![CDATA['
                               + '\n'.join(networks) + ']]></entry></smc2>', encoding='utf-8')
        with self.trace.open('a', encoding='utf-8') as stream:
            stream.write('2,1700000000024000000,30\n')
        self.cli('trace')
        pairs = self.read('trace_source_evidence.json')['pairs']
        evidence = pairs['Cell.Phase[4]=10|||Cell.Phase[4]=20']
        self.assertEqual((evidence['relation'], evidence['description']), ('unresolved', '等待夹具到位'))
        self.assertEqual(pairs['Cell.Phase[4]=20|||Cell.Phase[4]=30']['description'], '')
        self.assertEqual(self.read('workflow_status.json')['a_sourced'], 1)

    def test_existing_trace_workbook_export_flag_is_compatible(self):
        from gen_workstation_xlsx import main as export
        from openpyxl import load_workbook

        self.cli('trace')
        tables = []
        for flag in ('--pairs', '--prediction'):
            workbook = self.root / f'{flag[2:]}.xlsx'
            with patch('sys.argv', ['gen_workstation_xlsx.py', flag, str(self.out / 'trace_collection_pairs.csv'),
                                    '--out', str(workbook)]), redirect_stdout(io.StringIO()):
                export()
            with workbook.open('rb') as stream:
                book = load_workbook(stream, read_only=True)
                tables.append(list(book.active.values))
                book.close()
        self.assertEqual(*tables)
        no_duration = self.root / 'pairs_without_duration.csv'
        no_duration.write_text('开始标签,结束标签\nCell.Phase[4]=10,Cell.Phase[4]=20\n', encoding='utf-8')
        with patch('sys.argv', ['gen_workstation_xlsx.py', '--prediction', str(no_duration)]):
            with self.assertRaisesRegex(SystemExit, '中位数时长ms'):
                export()

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
