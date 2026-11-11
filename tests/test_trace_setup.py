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
        self.source.write_text(f'<project>{header}<pou name="Run"><body>{body}</body></pou></project>',
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
        self.assertEqual({p.name for p in self.out.iterdir()}, {'static_variables.txt', 'workflow_status.json'})
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
