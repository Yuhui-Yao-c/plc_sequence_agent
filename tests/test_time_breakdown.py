import csv
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from plc_trace.description.action_describe import cmd_a, cmd_validate
from plc_trace.description.trace_description_report import waiting_role
from plc_trace.output.gen_workstation_xlsx import generate as workbook
from plc_trace.trace.trace_to_pairs import generate, load_trace_timing


class TimeBreakdownTests(unittest.TestCase):
    def test_template_and_row_evidence_from_xml_and_real_trace_format(self):
        comments = ('待料', '堵料', '等待夹具到位', '检查是否堵料', '气缸前往等待位', '堵料报警复位', '备料',
                    '清除待料状态', '待料或堵料', '等待上游供料', '等待下游接料', '')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source, record = root / 'source.xml', root / 'actual.trace'
            source.write_text('<project><types><pous><pou name="Cell" pouType="program"><body><ST>' +
                              '\n'.join(f'IF S[{i}]=10 THEN ' + (f'//{c}\n' if c else '') +
                                        f'S[{i}]:=20; END_IF;' for i, c in enumerate(comments)) +
                              '</ST></body></pou></pous></types></project>', encoding='utf-8')
            record.write_text('<Trace><TraceConfiguration><Single Type="bool" Name="MicroSeconds">True</Single>'
                              '</TraceConfiguration><TraceData><TraceRecord>' +
                              ''.join(f'<TraceVariable VarName="S[{i}]"><Values>10,20</Values>'
                                      '<Timestamps>1000,208974</Timestamps></TraceVariable>' for i in range(len(comments)))
                              + '</TraceRecord></TraceData></Trace>', encoding='utf-16')
            run = root / 'results'
            pairs = generate(record, run, source=source)['outputs']['pairs_csv']
            cmd_a(run)
            cmd_validate(run)
            stats = workbook(pairs, run=str(run), sheet='trace步序采集表')
            self.assertEqual(load_trace_timing(run)[0]['S[0]=10', 'S[0]=20'], 207.974)
            with open(run / 'trace_segments.csv', encoding='utf-8-sig') as stream:
                self.assertEqual(next(csv.DictReader(stream))['时长ms'], '207.974')
            with open(stats['out'], 'rb') as stream:
                book = load_workbook(stream)
            ws = book.active
            self.assertEqual(ws.title, '时序拆解')
            self.assertEqual([c.value for c in ws[2]], [
                '构成要素', '动作时间', '完成时间', '动作描述', '描述来源', '时长（ms）', '目标时长',
                '开始标签', '结束标签', '动作号', '同步号', '工序号', '等待位标识（1：等待步）', '备注', '证据链'])
            self.assertEqual((ws['A1'].value, ws['H1'].value, ws.freeze_panes), ('动作分解', 'tag', 'A3'))
            for merge in ('H1:I1', 'B1:C1'):
                self.assertIn(merge, ws.merged_cells)
            self.assertTrue(all(ws.column_dimensions[letter].hidden for letter in 'BC'))
            rows = {r[7].value: r for r in ws.iter_rows(min_row=3) if r[7].value}
            self.assertEqual(len(rows), len(comments))
            for i, expected in enumerate(comments):
                row = rows[f'S[{i}]=10']
                self.assertEqual(row[3].value, expected or None)
                self.assertEqual(row[4].value, '注释' if expected else None)
                self.assertEqual(row[5].value, 207.974)
                self.assertTrue(all(row[col].value is None for col in (1, 2, 6, 9, 10, 11)))
                chain = row[14].value
                for part in ('样本索引 1', '207.974 ms', f'S[{i}]=10 → S[{i}]=20'):
                    self.assertIn(part, chain)
                self.assertIn('XML行' if expected else '动作描述待核', chain)
                self.assertEqual(row[12].value, 1 if i in (0, 1, 2, 9, 10) else None)
                for cell in row[3:]:
                    if i in (0, 1, 9, 10):
                        self.assertEqual(cell.fill.fgColor.rgb[-6:], 'FFFF00' if i in (0, 9) else '969696')
                    else:
                        self.assertIsNone(cell.fill.patternType)
            book.close()

            # Standalone re-render of an A-only preparation uses this run's original references.
            (run / 'action_desc_final.csv').unlink()
            restored = workbook(pairs, run=str(run), sheet='trace步序采集表')
            with open(restored['out'], 'rb') as stream:
                book = load_workbook(stream)
            cells = next(r for r in book.active.iter_rows(min_row=3) if r[7].value == 'S[0]=10')
            self.assertEqual((cells[3].value, cells[4].value, cells[12].value), ('待料', '注释', 1))
            self.assertIn('XML行', cells[14].value)
            book.close()

    def test_only_the_documented_positive_material_gate_gets_a_colour(self):
        ref = 'TX1'
        fact = {'id': ref, 'local': True, 'category': 'step_condition', 'subjects': [
            {'symbol': 'Cell.A1', 'label': '下游允许接料'}],
            'data': {'guard_complete': True, 'guard': '(S[9]=7) AND Cell.A1'}}
        info = {'relation': 'direct', 'direct': [{}], 'description_context': {'evidence': [fact]}}
        candidate = {'clause_evidence': [('等待接料许可', [ref])]}
        clause = {'source_ref': ref, 'condition_use': 'wait', 'subject_symbol': 'Cell.A1',
                  'material_flow': {'side': 'downstream', 'peer_quote': '下游', 'handoff_quote': '允许接料'}}
        def classify():
            return waiting_role(info, 'S[9]=7', candidate, {'actions': [clause]})
        self.assertEqual(classify()[0], '堵料')
        self.assertIn(ref, classify()[1])
        fact['subjects'][0]['label'] = '来件单元七：玻璃载具交付就绪'
        self.assertEqual(classify()[0], '等待')
        clause['material_flow'] = {'side': 'upstream', 'peer_quote': '来件单元七', 'handoff_quote': '玻璃载具交付就绪'}
        self.assertEqual(classify()[0], '待料')
        for guard in ('S[9]=7 AND NOT Cell.A1', 'S[8]=7 AND Cell.A1', 'S[9]=7 OR Cell.A1'):
            fact['data']['guard'] = guard
            self.assertEqual(classify()[0], '等待')
        clause['condition_use'] = 'check'
        self.assertEqual(classify()[0], '')
        info.update(description='待料', description_refs=['TC1'], relation='ambiguous_scope')
        self.assertEqual(classify()[0], '')
