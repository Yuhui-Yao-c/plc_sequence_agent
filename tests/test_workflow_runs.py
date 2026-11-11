import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from plc_trace import __version__
from plc_trace.description.action_describe import cmd_validate
from plc_trace.cli import main
from plc_trace.runtime.workflow import create, lock_run_dir, pipeline_fingerprint


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

    def test_run_fingerprint_covers_nested_code_resources_and_rules(self):
        project = self.root / 'project'
        package = project / 'plc_trace'
        resources = package / 'resources'
        tracked = {
            package / 'source' / 'parser.py': 'parser version one',
            resources / 'templates' / 'capture.xml': '<capture/>',
            resources / 'action_wordbook.json': '{}',
            project / '.opencode' / 'agents' / 'plc-sequence.md': 'agent rules',
            project / '.opencode' / 'skills' / 'example' / 'SKILL.md': 'skill rules',
            project / 'VERSION': '10.1.0',
        }
        for path, contents in tracked.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding='utf-8')
        with patch('plc_trace.runtime.workflow.PROJECT_ROOT', project), \
             patch('plc_trace.runtime.workflow.PACKAGE_ROOT', package), \
             patch('plc_trace.runtime.workflow.RESOURCE_ROOT', resources):
            before = pipeline_fingerprint()
            for path, contents in tracked.items():
                with self.subTest(path=path.relative_to(project)):
                    path.write_text(contents + '\nchanged', encoding='utf-8')
                    self.assertNotEqual(pipeline_fingerprint(), before)
                    path.write_text(contents, encoding='utf-8')
                    self.assertEqual(pipeline_fingerprint(), before)
            test = project / 'tests' / 'test_example.py'
            test.parent.mkdir()
            test.write_text('test-only change', encoding='utf-8')
            self.assertEqual(pipeline_fingerprint(), before)

    def test_version_query_works_without_a_task_from_an_external_directory(self):
        project = Path(__file__).resolve().parents[1]
        expected = (project / 'VERSION').read_text(encoding='utf-8').strip()
        self.assertEqual(__version__, expected)
        result = subprocess.run([sys.executable, str(project / 'plc_cli.py'), '--version'],
                                cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'plc_cli.py {expected}')
        self.assertEqual(result.stderr, '')
        self.assertFalse(self.out.exists())

    def shorthand(self, value, marker, *suffix):
        cli = Path(__file__).resolve().parents[1] / 'plc_cli.py'
        return subprocess.run([sys.executable, '-X', 'utf8', str(cli), str(self.source),
                               str(value), marker, *suffix], cwd=self.root,
                              capture_output=True, encoding='utf-8', check=False)

    def test_trace_shorthand_creates_process_runs_under_selected_root(self):
        source = self.root / 'source' / '包mylar' / 'PLC.xml'
        source.parent.mkdir(parents=True)
        self.source.rename(source)
        self.source = source
        output_root = self.root / '实测 输出'
        output_root.mkdir()
        sentinel = output_root / 'keep.txt'
        sentinel.write_text('other work', encoding='utf-8')
        self.out = output_root / '包mylar_trace'
        first = self.shorthand(self.trace, '-t', '-output', output_root.name)
        self.assertEqual(first.returncode, 0, first.stderr)
        status = self.read('workflow_status.json')
        self.assertEqual(status['mode'], 'trace')
        self.assertEqual(Path(status['out']), self.out.resolve())
        self.assertTrue((self.out / 'trace_collection_pairs.csv').is_file())

        repeat = self.shorthand(self.trace, '-t', '-output', output_root.name)
        self.assertEqual(repeat.returncode, 0, repeat.stderr)
        second = json.loads((output_root / '包mylar_trace_2' / 'workflow_status.json').read_bytes())
        self.assertNotEqual(second['run_id'], status['run_id'])
        self.assertEqual(self.read('workflow_status.json')['run_id'], status['run_id'])
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'other work')
        self.assertFalse((output_root / 'trace_collection_pairs.csv').exists())
        self.assertFalse((self.root / 'outputs').exists())

        self.cli('trace', '--resume')
        self.assertTrue(self.read('workflow_status.json')['preparation_reused'])
        self.assertEqual(self.read('workflow_status.json')['run_id'], status['run_id'])

        other_source = self.root / 'source' / '超声' / 'PLC.xml'
        other_source.parent.mkdir()
        other_source.write_bytes(self.source.read_bytes())
        self.source = other_source
        other = self.shorthand(self.trace, '-t', '-output', output_root.name)
        self.assertEqual(other.returncode, 0, other.stderr)
        self.assertTrue((output_root / '超声_trace' / 'trace_collection_pairs.csv').is_file())

    def test_shorthand_defaults_keep_trace_and_setup_separate(self):
        occupied = self.root / 'outputs' / '工序_trace'
        occupied.mkdir(parents=True)
        sentinel = occupied / 'keep.txt'
        sentinel.write_text('prior run', encoding='utf-8')
        trace = self.shorthand(self.trace, '-t')
        self.assertEqual(trace.returncode, 0, trace.stderr)
        self.assertTrue((occupied.with_name('工序_trace_2') / 'trace_collection_pairs.csv').is_file())
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'prior run')

        source = self.source.read_text(encoding='utf-8').replace(
            '<project>', '<project><fileHeader productName="InoProShop"/>', 1)
        self.source.write_text(source, encoding='utf-8')
        setup = self.shorthand('Cell.Phase[*]', '--trace-setup')
        self.assertEqual(setup.returncode, 0, setup.stderr)
        self.out = occupied.with_name('工序_trace_setup')
        self.assertEqual(self.read('workflow_status.json')['mode'], 'trace_setup')
        variables = (self.out / 'static_variables.txt').read_bytes()
        self.assertEqual(variables, b'Cell.Phase[4]\n')
        configuration = (self.out / 'PLC_trace_config.trace').read_bytes()

        custom_root = self.root / '配置 输出'
        custom_root.mkdir()
        reserved = custom_root / '工序_trace_setup'
        reserved.write_text('existing file', encoding='utf-8')
        custom = self.shorthand('Cell.Phase[*]', '--trace-setup', '-output', custom_root.name)
        self.assertEqual(custom.returncode, 0, custom.stderr)
        self.out = custom_root / '工序_trace_setup_2'
        self.assertEqual(Path(self.read('workflow_status.json')['out']), self.out.resolve())
        self.assertEqual((self.out / 'static_variables.txt').read_bytes(), variables)
        self.assertEqual((self.out / 'PLC_trace_config.trace').read_bytes(), configuration)
        self.assertEqual(reserved.read_text(encoding='utf-8'), 'existing file')
        self.assertFalse((custom_root / 'static_variables.txt').exists())

    def test_capture_multiple_patterns_in_shorthand_and_repeated_cli_options(self):
        self.source.write_text('<project><fileHeader productName="InoProShop"/>'
                               '<pou name="Run"><body><ST>Cell.Phase[4] := 10; Aux.Stage := 1;'
                               '</ST></body></pou></project>', encoding='utf-8')
        cli = Path(__file__).resolve().parents[1] / 'plc_cli.py'
        patterns = ['Cell.Phase[*]', r're:Aux\.Stage', 'Cell.Phase[4]']
        selected_root = self.root / '采集 输出'
        shorthand = subprocess.run(
            [sys.executable, '-X', 'utf8', str(cli), str(self.source), *patterns,
             '--trace-setup', '-output', str(selected_root)], cwd=self.root,
            capture_output=True, encoding='utf-8', check=False)
        self.assertEqual(shorthand.returncode, 0, shorthand.stderr)
        expected = selected_root / '工序_trace_setup'
        status = json.loads((expected / 'workflow_status.json').read_bytes())
        self.assertEqual(status['patterns'], patterns)
        self.assertEqual(status['static_variables'], 2)
        self.assertEqual(Path(status['out']), expected)
        full = ['plc_cli.py', 'trace-setup', '--source', str(self.source), '--out', str(self.out)]
        for pattern in patterns:
            full += ['--pattern', pattern]
        with patch('sys.argv', full), redirect_stdout(io.StringIO()):
            main()
        self.assertEqual((expected / 'static_variables.txt').read_bytes(),
                         (self.out / 'static_variables.txt').read_bytes())
        self.assertEqual((expected / 'PLC_trace_config.trace').read_bytes(),
                         (self.out / 'PLC_trace_config.trace').read_bytes())
        invalid = subprocess.run([sys.executable, str(cli), str(self.source),
                                  str(self.trace), str(self.trace), '-t'], cwd=self.root,
                                 capture_output=True, check=False)
        self.assertEqual(invalid.returncode, 2)

    def test_trace_shorthand_requires_one_complete_output_suffix(self):
        for marker, value in (('-t', self.trace), ('--trace-setup', 'Cell.Phase[*]')):
            for suffix in (('-output',), ('-output', ''), ('-output', '--force'),
                           ('-output', 'first', '-output', 'second')):
                with self.subTest(marker=marker, suffix=suffix):
                    args = ['plc_cli.py', str(self.source), str(value), marker, *suffix]
                    with patch('sys.argv', args), redirect_stderr(io.StringIO()) as errors:
                        with self.assertRaises(SystemExit) as error:
                            main()
                    self.assertEqual(error.exception.code, 2)
                    self.assertIn('-output', errors.getvalue())
        self.assertFalse(self.out.exists())

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

        with patch('plc_trace.cli.pipeline_fingerprint', return_value='changed-version'):
            with self.assertRaisesRegex(SystemExit, '无法续跑'):
                self.cli('trace', '--resume')

    def test_trace_preparation_preserves_full_source_evidence(self):
        self.cli('trace')
        manifest = self.read('workflow_manifest.json')
        self.assertEqual(manifest['mode'], 'trace')
        self.assertEqual(manifest['version'], __version__)
        self.assertEqual(manifest['atomic_projection']['version'], __version__)
        self.assertEqual(self.read('workflow_status.json')['version'], __version__)
        self.assertGreater(manifest['candidate_count'], 0)
        for name in ('sequence_candidates.csv', 'indexed_candidates.csv', 'indexed_events.csv',
                     'source_transition_edges.csv', 'source_transition_graph.json',
                     'source_stage_candidates_filtered.csv', 'plcopen_action_trace.csv',
                     'prediction_candidate_pairs.csv', 'prediction_collection_pairs.csv',
                     'RESULT_SCHEMA.md'):
            with self.subTest(name=name):
                self.assertTrue((self.out / name).is_file())
        self.assertTrue(list((self.out / 'batches').glob('*.input.json')))
        self.assertTrue(list((self.out / 'batches_desc').glob('*.input.json')))

    def test_only_trace_and_capture_setup_are_public_commands(self):
        for mode in ('predict', 'evaluate', 'finish', 'coverage'):
            with self.subTest(mode=mode), patch('sys.argv', ['plc_cli.py', mode]):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        main()
                self.assertEqual(error.exception.code, 2)
        self.assertFalse(self.out.exists())

    def test_rebuild_protects_inputs_and_refuses_a_concurrent_writer(self):
        self.out.mkdir()
        (self.out / 'workflow_manifest.json').write_text('{}', encoding='utf-8')
        nested_source = self.out / 'PLC.xml'
        nested_source.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(ValueError, '输入分开'):
            create(nested_source, self.out, force=True)
        self.assertTrue(nested_source.is_file())
        self.assertEqual(list(self.root.glob('out.previous-*')), [])
        with lock_run_dir(self.out), self.assertRaisesRegex(SystemExit, '正在由其他工作流命令使用'):
            self.cli('trace', '--force')
