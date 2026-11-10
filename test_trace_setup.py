import csv
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from extract_step_variables import ExtractionError, extract
from plc_cli import main


SCRIPT = Path(__file__).with_name('extract_step_variables.py')


class TraceSetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / '工序 源码.xml'
        self.out = self.root / 'setup'

    def test_standalone_script_keeps_original_source_text_matching(self):
        self.source.write_text('''<project>
            <fileHeader productName="InoProShop"/>
            <ST><![CDATA[
                Cell . Phase [ 2 ] := 3;
                Cell.Phase[10] := 5;
                CELL.Phase[2] := 6;
                Outer.Cell.Phase[11] := 6;
                Cell.Phase[index] := 7;
                x := 'Cell.Phase[5]';
            ]]></ST>
            <expression>Cell.Phase&#91;3&#93;</expression>
            <!-- Cell.Phase[4] -->
        </project>''', encoding='utf-16')
        source_bytes = self.source.read_bytes()
        result = subprocess.run(
            [sys.executable, '-S', str(SCRIPT), '--source', str(self.source),
             '--pattern', 'Cell.Phase[*]', '--out', str(self.out)],
            check=True, text=True, capture_output=True,
        )
        names = ['Cell.Phase[2]', 'Cell.Phase[3]', 'Cell.Phase[4]', 'Cell.Phase[5]', 'Cell.Phase[10]']
        self.assertIn('动态/表达式引用：1', result.stdout)
        self.assertEqual((self.out / 'static_variables.txt').read_text(encoding='utf-8').splitlines(), names)
        config = self.out / '工序 源码.trace'
        xml = ET.parse(config)
        samples = xml.findall('.//TraceVariable')
        self.assertEqual([el.get('VarName') for el in samples], names)
        self.assertTrue(all(not el.findtext('Values') and not el.findtext('Timestamps') for el in samples))
        self.assertEqual({p.name for p in self.out.iterdir()}, {'static_variables.txt', config.name})
        self.assertEqual(self.source.read_bytes(), source_bytes)

    def test_cli_alias_forwards_full_name_regex_to_same_extractor(self):
        self.source.write_text('<project><ST>Cell.Phase[2]; Cell.Other[3];</ST></project>', encoding='utf-8')
        direct = self.root / 'direct'
        matcher = r'Cell\.Phase\[\d+\]'
        args = ['plc_cli.py', 'trace-setup', '--source', str(self.source),
                '--pattern', 're:' + matcher, '--out', str(self.out)]
        with patch('sys.argv', args), redirect_stdout(io.StringIO()):
            main()
        extract(self.source, matcher, direct, mode='regex')
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()},
                         {p.name: p.read_bytes() for p in direct.iterdir()})

    def test_omron_csv_preserves_declared_type_and_quotes_multi_index(self):
        self.source.write_text('''<project>
            <fileHeader companyName="OMRON"/>
            <Variable name="Cell">
                <Type><TypeName>ARRAY[0..5,0..5] OF DINT</TypeName></Type>
            </Variable>
            <ST>Cell[1,2] := 5; CellAux[1] := 1;</ST>
        </project>''', encoding='utf-8')
        result = extract(self.source, 'Cell*[*]', self.out)
        with (self.out / result['trace_file']).open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(result['trace_file'], '工序 源码.csv')
        self.assertEqual(rows[-1], ['Index', 'Date', 'ClockTime', 'RawTime', 'TraceSampID',
                                    'Cell[1,2]:DINT', 'CellAux[1]:INT'])

    def test_conflict_leaves_both_outputs_and_other_files_alone(self):
        self.source.write_text('<project><ST>Step[1] := 20;</ST></project>', encoding='utf-8')
        self.out.mkdir()
        existing = self.out / '工序 源码.trace'
        existing.write_bytes(b'prior config')
        unrelated = self.out / 'summary.json'
        unrelated.write_bytes(b'other workflow')
        with self.assertRaisesRegex(ExtractionError, '输出文件已存在'):
            extract(self.source, 'Step[*]', self.out)
        self.assertFalse((self.out / 'static_variables.txt').exists())
        self.assertEqual(existing.read_bytes(), b'prior config')
        extract(self.source, 'Step[*]', self.out, overwrite=True)
        self.assertEqual(unrelated.read_bytes(), b'other workflow')

    def test_overwrite_rolls_back_list_if_configuration_cannot_be_replaced(self):
        self.source.write_text('<project><ST>Step[1] := 20; Step[2] := 30;</ST></project>', encoding='utf-8')
        result = extract(self.source, 'Step[1]', self.out)
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        replace = Path.replace

        def block_config(staged, target):
            if target.name == result['trace_file']:
                raise PermissionError('配置正在被打开')
            return replace(staged, target)

        with patch.object(Path, 'replace', block_config), self.assertRaises(PermissionError):
            extract(self.source, 'Step[2]', self.out, overwrite=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_no_static_match_or_input_collision_leaves_source_alone(self):
        self.source.write_text('<project><ST>Step[index] := 20;</ST></project>', encoding='utf-8')
        with self.assertRaisesRegex(ExtractionError, '没有匹配的静态变量'):
            extract(self.source, 'Step[*]', self.out)
        self.assertFalse(self.out.exists())
        source = self.root / 'Source.csv'
        raw = b'<project><fileHeader companyName="OMRON"/><ST>Step[1];</ST></project>'
        source.write_bytes(raw)
        with self.assertRaisesRegex(ExtractionError, '拒绝覆盖源码'):
            extract(source, 'Step[*]', self.root, overwrite=True)
        self.assertEqual(source.read_bytes(), raw)
        self.assertFalse((self.root / 'static_variables.txt').exists())
