import csv
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from plc_trace import __version__
from plc_trace.capture.extract_step_variables import generate
from plc_trace.cli import main
from plc_trace.trace.trace_to_pairs import generate as analyze_trace


class TraceSetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / '工程.xml'
        self.out = self.root / 'setup'

    def plcopen(self, body, *, header='<fileHeader productName="InoProShop"/>', encoding='utf-8'):
        self.source.write_text(f'<project>{header}<pou name="Run"><body>{body}</body></pou>'
                               '<resource><task name="CaptureTask"><pouInstance name="Run"/></task></resource></project>',
                               encoding=encoding)

    def sysmac(self, body):
        self.source.write_text(
            '<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10">' + body + '</Project>', encoding='utf-8')

    def test_cli_inovance_uses_full_names_and_serializes_configuration_only(self):
        self.plcopen('''
            <ST><![CDATA[
                Cell . Phase [ INT#002 ] := 4;
                Cell.Phase[10] := 5;
                CELL.Phase[2] := 6;
                Outer.Cell.Phase[4] := 5;
                Cell.Phase[Map[Grid[8]]] := 5;
                // Cell.Phase[22]
                (* Cell.Phase[23] (* comment *) Cell.Phase[24] *)
                String := 'Cell.Phase[25]';
            ]]></ST>
            <LD>
              <contact><expression>Cell.Phase&#91;3&#93;</expression></contact>
              <comment><content>Cell.Phase[26]</content></comment>
            </LD>
            <!-- Cell.Phase[27] -->
        ''', encoding='utf-16')
        before = self.source.read_bytes()
        args = ['plc_cli.py', 'trace-setup', '--source', str(self.source), '--pattern', 'Cell.Phase[*]',
                '--out', str(self.out), '--task-name', 'CaptureTask']
        with patch('sys.argv', args), redirect_stdout(io.StringIO()) as output:
            main()
        report = json.loads(output.getvalue())
        self.assertEqual(report['version'], __version__)
        variables = ['Cell.Phase[2]', 'Cell.Phase[3]', 'Cell.Phase[10]']
        self.assertEqual((self.out / 'static_variables.txt').read_text(encoding='utf-8').splitlines(), variables)
        self.assertEqual(report['dynamic_variables'], ['Cell.Phase[Map[Grid[8]]]'])
        self.assertEqual(before, self.source.read_bytes())
        trace = Path(report['configuration'])
        self.assertTrue(trace.read_bytes().startswith((b'\xff\xfe', b'\xfe\xff')))
        xml = ET.parse(trace)
        self.assertEqual(xml.findtext(".//Single[@Name='TaskName']"), 'CaptureTask')
        self.assertEqual([el.text for el in xml.findall(".//Single[@Name='VariableName']")], variables)
        self.assertEqual([el.text for el in xml.findall(".//Single[@Name='guid']")],
                         [el.text for el in xml.findall(".//Single[@Name='ReferencedVarGuid']")])
        samples = xml.findall('.//TraceVariable')
        self.assertEqual([el.get('VarName') for el in samples], variables)
        self.assertTrue(all(not el.findtext('Values') and not el.findtext('Timestamps') for el in samples))
        with self.assertRaisesRegex(ValueError, '无采样数据'):
            analyze_trace(trace, self.root / 'analysis', source=self.source)

    def test_sysmac_scoped_types_and_quoted_multi_index_csv(self):
        self.sysmac('''
            <DataTypeDecl name="Machine"><UserDefinedTypeSpec>
                <Member name="Phase"><Type><TypeName>ARRAY[-1..3,0..8] OF DINT</TypeName></Type></Member>
            </UserDefinedTypeSpec></DataTypeDecl>
            <GlobalVars><Variable name="Cell"><Type><TypeName>Machine</TypeName></Type></Variable></GlobalVars>
            <Program name="Feed"><LocalVars>
                <Variable name="Phase"><Type><TypeName>UINT</TypeName></Type></Variable>
            </LocalVars><BodyContent><ST>Phase := 10;</ST>
                <FbdObject identifier="Cell.Phase[-1,2]"/>
            </BodyContent></Program>
        ''')
        report = generate(self.source, self.out, r're:(Cell|Feed)\.Phase.*')
        self.assertEqual(report['status'], 'done')
        self.assertEqual([(c['name'], c['type']) for c in report['channels']],
                         [('Cell.Phase[-1,2]', 'DINT'), ('Feed.Phase', 'UINT')])
        config = Path(report['configuration'])
        with config.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[-1], ['Index', 'Date', 'ClockTime', 'RawTime', 'TraceSampID',
                                    'Cell.Phase[-1,2]:DINT', 'Feed.Phase:UINT'])
        with self.assertRaisesRegex(ValueError, '无采样数据'):
            analyze_trace(config, self.root / 'analysis', source=self.source)
        multiple = generate(self.source, self.root / 'multiple',
                            ['Cell.Phase[-1,2]', 'Feed.Phase', 'Cell.Phase[-1,2]'])
        self.assertEqual(multiple['channels'], report['channels'])
        self.assertEqual(Path(multiple['configuration']).read_bytes(), config.read_bytes())
        with self.assertRaisesRegex(ValueError, '汇川'):
            generate(self.source, self.root / 'invalid', 'Feed.Phase', every_n_cycles=3)
        self.assertFalse((self.root / 'invalid').exists())

    def test_multiple_patterns_union_deduplicates_and_preserves_configuration(self):
        self.plcopen('<ST>Cell.Phase[2] := 10; Cell.Phase[10] := 20; Flow.Stage := 1; '
                     'Cell.Phase[Which] := 5;</ST>')
        patterns = ['Cell.Phase[*]', r're:Flow\.Stage', 'Cell.Phase[2]', 'Cell.Phase[*]']
        report = generate(self.source, self.out, patterns)
        self.assertEqual([c['name'] for c in report['channels']],
                         ['Cell.Phase[2]', 'Cell.Phase[10]', 'Flow.Stage'])
        self.assertEqual(report['patterns'], patterns[:3])
        self.assertIsNone(report['pattern'])
        self.assertEqual(report['dynamic_variables'], ['Cell.Phase[Which]'])
        single_pattern = r' re:Cell\.Phase\[.*\]|Flow\.Stage '
        single = generate(self.source, self.root / 'single', single_pattern)
        self.assertEqual(report['channels'], single['channels'])
        self.assertEqual(Path(report['configuration']).read_bytes(),
                         Path(single['configuration']).read_bytes())
        self.assertEqual(single['pattern'], single_pattern)

    def test_multiple_patterns_validate_every_selector_before_output(self):
        self.plcopen('<ST>Cell.Phase[2] := 10;</ST>')
        for patterns in ([], ['Cell.Phase[*]', ' '], ['Cell.Phase[*]', 're:.*'],
                         ['Cell.Phase[*]', 're:[']):
            with self.subTest(patterns=patterns), self.assertRaises((ValueError, re.error)):
                generate(self.source, self.out, patterns)
            self.assertFalse(self.out.exists())

    def test_multiple_patterns_respect_case_sensitive_matching(self):
        self.plcopen('<ST>Cell.Phase[2] := 10; Flow.Stage := 1;</ST>')
        patterns = ['cell.phase[*]', r're:Flow\.Stage']
        report = generate(self.source, self.out, patterns, case_sensitive=True)
        self.assertEqual([c['name'] for c in report['channels']], ['Flow.Stage'])
        insensitive = generate(self.source, self.root / 'insensitive', patterns)
        self.assertEqual([c['name'] for c in insensitive['channels']], ['Cell.Phase[2]', 'Flow.Stage'])

    def test_unsupported_and_missing_types_need_review(self):
        self.sysmac('''
            <GlobalVars>
                <Variable name="Flags"><Type><TypeName>BOOL</TypeName></Type></Variable>
                <Variable name="States"><Type><TypeName>ARRAY[0..2] OF INT</TypeName></Type></Variable>
            </GlobalVars>
            <Program name="Line"><BodyContent>
                <ST>Flags := TRUE; States[3] := 1; Unknown := 2;</ST>
            </BodyContent></Program>
        ''')
        report = generate(self.source, self.out, r're:Flags|States\[3\]|Unknown')
        self.assertEqual(report['status'], 'needs_review')
        self.assertIsNone(report['configuration'])
        self.assertEqual(set(report['unresolved_types']), {'Flags', 'States[3]', 'Unknown'})
        self.assertEqual({p.name for p in self.out.iterdir()}, {'static_variables.txt', 'workflow_status.json',
                                                               'evidence'})
        self.assertEqual([p.name for p in (self.out / 'evidence').iterdir()], ['unresolved_types.csv'])
        self.assertEqual(set(Path(report['trace_targets']).read_text().splitlines()), {'Flags', 'States[3]', 'Unknown'})
        rebuilt = generate(self.source, self.out, 'Unknown', default_type='DINT', force=True)
        self.assertEqual(rebuilt['status'], 'done')
        self.assertEqual(rebuilt['channels'][0]['type_source'], '用户指定')

    def test_force_stages_complete_replacement_and_protects_other_output(self):
        self.plcopen('<ST>Step[1] := 20; Step[2] := 30;</ST>')
        first = generate(self.source, self.out, 'Step[1]')
        before = Path(first['configuration']).read_bytes()
        with self.assertRaisesRegex(ValueError, '未找到'):
            generate(self.source, self.out, 'DoesNotExist', force=True)
        self.assertEqual(Path(first['configuration']).read_bytes(), before)
        self.assertEqual(list(self.root.glob('setup.previous-*')), [])
        generate(self.source, self.out, 'Step[2]', force=True)
        archive, = self.root.glob('setup.previous-*')
        self.assertEqual((archive / Path(first['configuration']).name).read_bytes(), before)
        other = self.root / 'trace-analysis'
        other.mkdir()
        (other / 'workflow_status.json').write_text('{"mode":"trace"}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '其他任务'):
            generate(self.source, other, 'Step[1]', force=True)

    def test_add_verified_members_to_same_reviewed_run_and_retire_partial_configuration(self):
        names = ['Left.Phase', 'Right.Phase', 'Known']
        self.sysmac('<GlobalVars>' + ''.join(
            f'<Variable name="{n}"><Type><TypeName>{t}</TypeName></Type></Variable>'
            for n, t in [('Left', 'Carrier'), ('Right', 'Feeder'), ('Known', 'INT')])
            + '</GlobalVars><Program name="Work"><BodyContent><ST>'
            + ' '.join(f'CASE {n} OF 1: {n}:=4; 4: {n}:=1; END_CASE;' for n in names)
            + '</ST></BodyContent></Program>')
        ref = self.root / 'verified_targets.txt'
        ref.write_text('\n'.join(names))
        args = {'reference': ref, 'choose_available': True}
        first = generate(self.source, self.out, **args)
        self.assertEqual(first['status'], 'scope_review_pending')
        inventory = json.loads((self.out / 'evidence/step_variable_discovery.json').read_bytes())
        scope = self.root / 'scope.json'
        scope.write_text(json.dumps({'inventory_sha256': inventory['inventory_sha256'], 'decisions': [
            {'name': r['name'], 'role': 'uncertain', 'reason': '独立地址可核对', 'basis': ['t1']}
            for r in inventory['candidates'] if r['eligibility'] == 'scope_required']}))
        partial = generate(self.source, self.out, scope_results=scope, **args)
        self.assertEqual(partial['configured_variables'], 1)
        self.assertFalse(Path(first['preview_configuration']).exists())
        self.assertFalse(partial['configuration_complete'])
        self.assertEqual(partial['reference_selected_variables'], 3)
        self.assertEqual(partial['reference_only_variables'], 3)

        supplement = self.out / 'member_types.csv'
        supplement.write_text('结构类型,成员,数据类型\nCarrier,Phase,UINT\n')
        second = generate(self.source, self.out, member_types=supplement, **args)
        self.assertEqual(second['configured_variables'], 2)
        self.assertEqual(second['out'], first['out'])
        self.assertTrue(supplement.exists())
        self.assertEqual(second['scope_input_sha256'], partial['scope_input_sha256'])
        supplement.write_text('结构类型,成员,数据类型\nFeeder,Phase,DINT\n')
        final = generate(self.source, self.out, member_types=supplement, **args)
        self.assertEqual({c['name']: c['type'] for c in final['channels']},
                         {'Left.Phase': 'UINT', 'Right.Phase': 'DINT', 'Known': 'INT'})
        self.assertTrue(final['configuration_complete'])
        self.assertEqual(list(self.out.glob('*trace_config*.csv')), [Path(final['configuration'])])
        self.assertFalse((self.out / 'missing_member_types.csv').exists())
        self.assertTrue(supplement.exists())
        self.assertEqual(generate(self.source, self.out, scope_results=scope, **args), final)
        self.assertFalse((self.root / 'setup_2').exists())
        supplement.write_text('结构类型,成员,数据类型\nCarrier,Phase,LINT\n')
        with self.assertRaisesRegex(ValueError, '冲突'):
            generate(self.source, self.out, member_types=supplement, **args)
        self.assertEqual(generate(self.source, self.out, **args), final)

    def test_same_step_wait_in_a_reference_does_not_promote_its_child_release_state(self):
        from plc_trace.capture.channel_reference import read_reference
        phases = [{'identity': name, 'sequence_witness': True, 'transitions': [], 'control_links': [],
                   'eligibility': 'scope_required'} for name in ('cell.phase', 'scan.phase', 'clamp.phase')]
        files = self.root / 'timing.csv'
        files.write_text('开始标签,结束标签\n'
                         'Cell.Phase=7,Cell.Phase=7 AND Scan.Phase=100\n'
                         'Scan.Phase=42 AND Clamp.Phase=3,Finished\n'
                         'Cell.Phase=9,Clamp.Phase=4\n')
        selected, _, unresolved = read_reference(files, phases)
        self.assertEqual(set(selected), {'cell.phase', 'clamp.phase'})
        self.assertEqual(unresolved, [{'row': 3, 'operands': ['clamp.phase', 'scan.phase']}])

    def test_legacy_scope_can_be_upgraded_in_place_without_rewriting_other_judgments(self):
        names = ['ClampProgress', 'FeedProgress']
        self.sysmac('<GlobalVars>' + ''.join(
            f'<Variable name="{n}"><Type><TypeName>INT</TypeName></Type></Variable>' for n in names)
            + '</GlobalVars><Program name="Run"><BodyContent><ST>'
            + ' '.join(f'CASE {n} OF 2: IF Ready THEN {n}:=8; END_IF; 8: {n}:=2; END_CASE;' for n in names)
            + '</ST></BodyContent></Program>')
        generate(self.source, self.out)
        inventory = json.loads((self.out / 'evidence/step_variable_discovery.json').read_bytes())
        old = []
        for n in names:
            old.append({'name': n, 'role': 'component_sequence', 'basis': ['t1'], 'reason': '机械动作',
                        'use': 'production', 'boundaries': [{'action': '送料', 'write': 't1', 'via': 'signal:Ready'}]})
        first = self.root / 'review.json'
        review = {'inventory_sha256': inventory['inventory_sha256'], 'decisions': old}
        first.write_text(json.dumps(review))
        empty = generate(self.source, self.out, scope_results=first)
        self.assertEqual((empty['status'], empty['legacy_timing_reviews'], empty['static_variables']),
                         ('needs_review', 2, 0))
        self.assertIsNone(empty['configuration'])
        self.assertFalse(list(self.out.glob('*trace_config*.csv')))
        old[-1]['timing_basis'] = {'scope': 'workstation', 'operation': '送料', 'boundary_write': 't1'}
        first.write_text(json.dumps(review))
        partial = generate(self.source, self.out, scope_results=first)
        self.assertEqual((partial['legacy_timing_reviews'], partial['static_variables']), (1, 1))
        original_config = Path(partial['configuration']).read_bytes()
        old[0]['timing_basis'] = {'scope': 'workstation', 'operation': '夹紧', 'boundary_write': 't1'}
        old[-1]['reason'] = '更改已有判断'
        first.write_text(json.dumps(review))
        with self.assertRaisesRegex(ValueError, '补判只能'):
            generate(self.source, self.out, scope_results=first)
        self.assertEqual(Path(partial['configuration']).read_bytes(), original_config)
        old[-1]['reason'] = '机械动作'
        first.write_text(json.dumps(review))
        result = generate(self.source, self.out, scope_results=first)
        self.assertEqual((result['static_variables'], result['legacy_timing_reviews']), (2, 0))
        self.assertEqual(generate(self.source, self.out, scope_results=first), result)

    def test_shorthand_reuses_only_an_identical_source_options_and_program(self):
        import subprocess
        import sys
        self.plcopen('<ST>Step[1] := 20; Step[2] := 30;</ST>')
        project = Path(__file__).resolve().parents[1]
        root = self.root / '输出'
        def invoke(pattern):
            result = subprocess.run([sys.executable, str(project/'plc_cli.py'), str(self.source),
                                     pattern, '--trace-setup', '-output', str(root)],
                                    cwd=self.root, capture_output=True, text=True, check=True)
            return Path(json.loads(result.stdout)['out'])
        original = self.source.read_bytes()
        first = invoke('Step[1]')
        self.assertEqual(invoke('Step[1]'), first)
        second = invoke('Step[2]')
        self.assertNotEqual(first, second)
        self.assertEqual(invoke('Step[2]'), second)
        self.source.write_bytes(original.replace(b':= 20', b':= 40'))
        third = invoke('Step[1]')
        self.assertEqual(len({first, second, third}), 3)
        self.source.write_bytes(original)
        self.assertEqual(invoke('Step[1]'), first)
        with patch('plc_trace.capture.extract_step_variables.pipeline_fingerprint', return_value='changed'):
            fourth = generate(self.source, first, 'Step[1]', choose_available=True)
        self.assertEqual(set(root.iterdir()), {first, second, third, Path(fourth['out'])})

    def test_smc2_decodes_json_values_and_audits_malformed_input(self):
        network = {'CMT': '步序[99]', 'CLs': [{'Var': '步序[10]'}, {'TXT': '步序[2] := 20;'}]}
        payload = json.dumps(network, ensure_ascii=True)
        wrapped = '<smc2><entry type="json" name="Run"><![CDATA[%s]]></entry></smc2>'
        self.source.write_text(wrapped % payload, encoding='utf-8')
        report = generate(self.source, self.out, '步序[*]', default_type='INT')
        self.assertEqual([c['name'] for c in report['channels']], ['步序[2]', '步序[10]'])
        self.assertEqual(report['status'], 'done')
        self.source.write_text(wrapped % (payload + '\n{bad JSON'), encoding='utf-8')
        report = generate(self.source, self.out, '步序[*]', default_type='INT', force=True)
        self.assertEqual(report['status'], 'needs_review')
        self.assertTrue(report['parse_issues'])
        self.assertIsNone(report['configuration'])

    def test_unknown_xml_vendor_requires_explicit_configuration_format(self):
        self.plcopen('<ST>Phase := 10;</ST>', header='')
        with self.assertRaisesRegex(ValueError, '匹配空名称'):
            generate(self.source, self.out, '*')
        with self.assertRaisesRegex(ValueError, '指定 --format'):
            generate(self.source, self.out, 'Phase')
        self.assertFalse(self.out.exists())
        self.assertEqual(generate(self.source, self.out, 'Phase', capture_format='inovance')['status'], 'done')
