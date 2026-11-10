import json
import tempfile
import unittest
from pathlib import Path

import test_trace_to_pairs as fixtures
from action_describe import cmd_a, cmd_build, cmd_validate
from gen_workstation_xlsx import generate as workbook
from station_identity import source_station_displays
from trace_description_context import validate_context_result
from trace_to_pairs import generate
from utils import infer_proc_name


class TraceDescriptionTests(unittest.TestCase):
    FIRST = fixtures.SysmacRecordingTests.FIRST
    EPOCH = fixtures.SysmacRecordingTests.EPOCH
    write_part = fixtures.SysmacRecordingTests.write_part
    write_csv = fixtures.SysmacRecordingTests.write_csv
    read_csv = fixtures.SysmacRecordingTests.read_csv
    source_edges = fixtures.SysmacRecordingTests.source_edges
    command = fixtures.SysmacRecordingTests.command

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.trace = self.root / 'trace'
        self.trace.mkdir()
        self.run = self.root / 'run'
        self.run.mkdir()

    def record(self, start=10, end=30):
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, start), (1, end)])

    def prepare(self):
        stats = generate(self.trace, self.run, source='source/包mylar/PLC程序_202608251035.xml')
        cmd_a(self.run)
        cmd_build(self.run)
        evidence = json.loads((self.run / 'trace_source_evidence.json').read_text())['pairs']
        return stats, next(iter(evidence.values()))

    def comment(self, start, end, text, source='T1'):
        return {'开始标签': f'Step[1]={start}', '结束标签': f'Step[1]={end}',
                '证据POU': 'Program::Step', '证据节点': source, '网络序号': '1',
                '源码行号': '1', '动作描述': text, '状态': 'native_wired_transition'}

    def test_start_comment_survives_sampled_path_and_unresolved_successor(self):
        self.record()
        self.source_edges(self.run, [(10, 20), (20, 30)])
        self.write_csv(self.run / 'source_stage_candidates.csv', [
            self.comment(10, 20, '10-气缸夹紧'), self.comment(30, 40, '30-机器人取料', 'T3')])
        stats, info = self.prepare()
        self.assertEqual((info['relation'], info['description']), ('path_candidate', '气缸夹紧'))
        self.assertTrue(all(ref in {n['id'] for n in info['start_notes']} for ref in info['description_refs']))
        self.assertEqual(stats['unique_pairs'], 1)
        self.assertEqual(list((self.run / 'batches_desc').glob('*.input.json')), [])
        self.assertIn('起始步', self.read_csv(self.run / 'trace_collection_pairs.csv')[0]['备注'])
        self.record(10, 99)
        _, info = self.prepare()
        self.assertEqual((info['relation'], info['description']), ('unresolved', '气缸夹紧'))

    def test_workbook_groups_global_array_by_writing_program(self):
        names = ('Phase[1]', 'Phase[2]')
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 10, 10), (1, 20, 20)], channels=names)
        source = self.root / 'source.xml'
        source.write_text(
            '<project><types><pous>'
            '<pou name="Load" pouType="program"><body><ST>'
            'IF Phase[1]=10 THEN //载具上料\n Phase[1]:=20; END_IF;'
            '</ST></body></pou>'
            '<pou name="Unload" pouType="program"><body><ST>'
            'IF Phase[2]=10 THEN //载具卸料\n Phase[2]:=20; END_IF;'
            '</ST></body></pou>'
            '</pous></types></project>', encoding='utf-8')
        (self.run / 'variable_documentation.json').write_text(
            json.dumps({'all': {'Phase': '全线自动步状态'}}), encoding='utf-8')
        (self.run / 'station_identity.json').write_text(json.dumps({'Phase': {
            'code': 'Phase', 'display': '全线自动步状态', 'doc': '全线自动步状态', 'sort_key': [0, 0]
        }}), encoding='utf-8')
        stats = generate(self.trace, self.run, source=source)
        pairs = stats['outputs']['pairs_csv']
        self.assertEqual({r['构成要素'] for r in self.read_csv(pairs)}, {'Load', 'Unload'})
        result = workbook(pairs, run=str(self.run), sheet='trace步序采集表')
        self.assertEqual(result['workstation_groups'], 2)
        from openpyxl import load_workbook

        book = load_workbook(result['out'])
        self.addCleanup(book.close)
        sheet = book.active
        self.assertEqual((sheet['B4'].value, sheet['B6'].value), ('Load', 'Unload'))
        self.assertEqual(sheet.max_row, 7)
        self.assertIn('B4:B5', sheet.merged_cells)
        self.assertIn('B6:B7', sheet.merged_cells)

    def test_trace_workstation_splits_full_channels_and_keeps_one_total(self):
        from openpyxl import load_workbook

        names = ('Line.Flow[10]', 'Line.Flow[2]', 'Line.Seq[2]', 'Solo')
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH,
                        [(0, 20, 10, 50, 0), (1, 30, 20, 60, 10), (2, 30, 30, 60, 10)], channels=names)
        source = self.root / 'source.xml'
        source.write_text(
            '<project><types><pous>'
            '<pou name="CellA" pouType="program"><body><ST>'
            'IF Line.Flow[2]=10 THEN Line.Flow[2]:=20; END_IF; '
            'IF Line.Flow[2]=20 THEN Line.Flow[2]:=30; END_IF; '
            'IF Line.Flow[10]=20 THEN Line.Flow[10]:=30; END_IF; '
            'IF Line.Seq[2]=50 THEN Line.Seq[2]:=60; END_IF;'
            '</ST></body></pou>'
            '<pou name="CellB" pouType="program"><body><ST>'
            'IF Solo=0 THEN Solo:=10; END_IF;'
            '</ST></body></pou>'
            '</pous></types></project>', encoding='utf-8')
        pairs = generate(self.trace, self.run, source=source)['outputs']['pairs_csv']
        result = workbook(pairs, run=str(self.run), sheet='trace步序采集表')
        book = load_workbook(result['out'])
        self.addCleanup(book.close)
        sheet = book.active
        self.assertEqual(result['rows'], 5)
        self.assertEqual(result['workstation_groups'], 2)
        self.assertEqual([sheet[f'B{r}'].value for r in (4, 8, 11)],
                         ['Line\nLine.Flow[2]', 'Line\nLine.Flow[10]', 'Line\nLine.Seq[2]'])
        self.assertEqual([sheet[f'G{r}'].value for r in (4, 5, 8, 11, 13)],
                         ['Line.Flow[2]=10', 'Line.Flow[2]=20', 'Line.Flow[10]=20', 'Line.Seq[2]=50', 'Solo=0'])
        self.assertIn('B4:B5', sheet.merged_cells)
        for row in (6, 7, 9, 10):
            self.assertTrue(all(cell.value is None for cell in sheet[row]))
        self.assertEqual((sheet['B12'].value, sheet['C12'].value, sheet['E12'].value), ('Line', '行时长合计', 48))
        self.assertEqual((sheet['B13'].value, sheet['C14'].value, sheet['E14'].value), ('Solo', '行时长合计', 12))

    def test_workbook_preserves_formula_like_source_comment_as_text(self):
        from openpyxl import load_workbook

        comment = '====判断步===='
        self.record(0, 100)
        source = self.root / 'source.xml'
        source.write_text(
            '<project><types><pous><pou name="Load" pouType="program"><body><ST>'
            f'IF Step[1]=0 THEN //{comment}\n Step[1]:=100; END_IF;'
            '</ST></body></pou></pous></types></project>', encoding='utf-8')
        pairs = generate(self.trace, self.run, source=source)['outputs']['pairs_csv']
        cmd_a(self.run)
        cmd_validate(self.run)
        self.assertEqual(self.read_csv(self.run / 'action_desc_final.csv')[0]['动作描述'], comment)

        result = workbook(pairs, run=str(self.run), sheet='trace步序采集表')
        book = load_workbook(result['out'], read_only=True, data_only=True)
        self.addCleanup(book.close)
        self.assertEqual(book.active['C4'].value, comment)
        self.assertEqual(book.active['D4'].value, '注释')
        self.assertEqual(book.active['E4'].value, int(self.read_csv(pairs)[0]['中位数时长ms']))
        self.assertIsNone(book.active['J4'].value)

    def test_trace_rows_share_one_height_and_link_to_full_text(self):
        from openpyxl import load_workbook
        from gen_workstation_xlsx import STEP_HEIGHT

        path = self.run / 'trace_collection_pairs.csv'
        long_note = '\n'.join(f'第{i}条原始条件 Step[1]=10 AND Ready' for i in range(1, 9))
        self.write_csv(path, [
            {'开始标签': 'Step[1]=10', '结束标签': 'Step[1]=20', '构成要素': 'Feed',
             '中位数时长ms': 12, '备注': long_note, '源码关系': 'direct', '动作描述': '转入进料'},
            {'开始标签': 'Step[1]=20', '结束标签': 'Step[1]=10', '构成要素': 'Feed',
             '中位数时长ms': 24, '备注': '简短备注', '源码关系': 'direct', '动作描述': '复位'},
        ])
        result = workbook(path, run=self.run)
        book = load_workbook(result['out'])
        self.addCleanup(book.close)
        sheet = book.active
        self.assertEqual([sheet.row_dimensions[r].height for r in (4, 5)], [STEP_HEIGHT, STEP_HEIGHT])
        self.assertEqual(sheet['I4'].value, long_note)
        self.assertEqual(sheet['I4'].hyperlink.location, "'文本详情'!E2")
        self.assertIsNone(sheet['I5'].hyperlink)
        self.assertEqual(book['文本详情']['E2'].value, long_note)
        self.assertEqual(book['文本详情']['A2'].hyperlink.location, "'trace步序采集表'!I4")

    def test_vague_notes_get_only_their_own_routine_and_control_details(self):
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH,
                        [(0, 0, 20, 20), (1, 20, 0, 0), (2, 0, 0, 0)],
                        channels=('Step[1]', 'Step[2]', 'Step[3]'))
        source = self.root / 'source.xml'
        source.write_text(
            '<project><types><pous>'
            '<pou name="托盘高度测量" pouType="program"><body><ST>'
            'IF Step[1]=0 AND HeightRequest THEN //====判断步====\n Step[1]:=10; END_IF;\n'
            'IF Step[1]=10 AND Ready THEN Step[1]:=20; END_IF;\n'
            'IF Step[1]=20 AND NOT HeightBusy THEN //20：测量完成\n Step[1]:=0; END_IF;'
            '</ST></body></pou>'
            '<pou name="极片厚度测量" pouType="program"><body><ST>'
            'IF Step[2]=20 AND NOT ThicknessBusy THEN //20：测量完成\n Step[2]:=0; END_IF;'
            '</ST></body></pou>'
            '<pou name="输送机构" pouType="program"><body><ST>'
            'IF Step[3]=20 THEN //====定位轴上升至检测位====\n Step[3]:=0; END_IF;'
            '</ST></body></pou></pous></types></project>', encoding='utf-8')
        pairs = generate(self.trace, self.run, source=source)['outputs']['pairs_csv']
        cmd_a(self.run)
        final = {r['开始标签']: r for r in self.read_csv(cmd_validate(self.run))}
        self.assertEqual(final['Step[1]=0']['动作描述'], '====判断步====')
        first = final['Step[1]=0']['注释补充']
        self.assertIn('托盘高度测量', first)
        self.assertIn('候选路径 0 → 10 → 20', first)
        self.assertIn('HeightRequest', first)
        self.assertNotIn('Ready', first)
        self.assertEqual(final['Step[1]=20']['动作描述'], '20：测量完成')
        self.assertIn('HeightBusy', final['Step[1]=20']['注释补充'])
        second = final['Step[2]=20']['注释补充']
        self.assertIn('极片厚度测量', second)
        self.assertIn('ThicknessBusy', second)
        self.assertNotIn('HeightBusy', second)
        self.assertEqual(final['Step[3]=20']['注释补充'], '')
        self.assertEqual(final['Step[3]=20']['动作描述'], '====定位轴上升至检测位====')

        from openpyxl import load_workbook
        rendered = workbook(pairs, run=str(self.run), sheet='trace步序采集表')
        with open(rendered['out'], 'rb') as stream:
            book = load_workbook(stream, read_only=True)
            sheet_rows = {r[6]: r for r in book.active.iter_rows(min_row=4, values_only=True) if r[6]}
            book.close()
        self.assertIn(first, sheet_rows['Step[1]=0'][8])
        self.assertIn(second, sheet_rows['Step[2]=20'][8])
        self.assertIsNone(sheet_rows['Step[2]=20'][9])

    def test_source_station_name_is_used_for_csv_and_existing_workbook_export(self):
        from openpyxl import load_workbook

        name = 'Dock4_进料单元'
        controller = 'LoaderView.Status[1]'
        alias = f'LoaderView_{name}'
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 0), (1, 20)], channels=(controller,))
        source = self.root / 'source.xml'
        source.write_text(
            f'<project><types><pous><pou name="{name}" pouType="program"><body><ST>'
            f'IF {controller}=0 THEN //载具进料\n {controller}:=20; END_IF;'
            '</ST></body></pou></pous></types></project>', encoding='utf-8')
        (self.run / 'station_identity.json').write_text(json.dumps({'LoaderView': {
            'code': 'LoaderView', 'doc': name, 'display': alias, 'sort_key': [4, 0],
        }}), encoding='utf-8')

        path = generate(self.trace, self.run, source=source)['outputs']['pairs_csv']
        row, = self.read_csv(path)
        self.assertEqual(row['构成要素'], name)
        self.assertEqual(row['控制器'], controller)

        legacy = self.run / 'legacy-pairs.csv'
        self.write_csv(legacy, [{**row, '构成要素': alias}])
        result = workbook(str(legacy), run=str(self.run), sheet='trace步序采集表')
        book = load_workbook(result['out'], read_only=True, data_only=True)
        self.addCleanup(book.close)
        self.assertEqual(book.active['B4'].value, name)
        self.assertEqual(book.active['G4'].value, f'{controller}=0')

    def test_same_documented_name_does_not_merge_station_groups(self):
        doc = 'Dock_进料单元'
        identities = {code: {'code': code, 'doc': doc, 'display': f'{code}_{doc}'}
                      for code in ('FeederA', 'FeederB')}
        groups = [(identities['FeederA']['display'], 'FeederA.Status', f'LineA::{doc}::Run'),
                  (identities['FeederB']['display'], 'FeederB.Status', f'LineB::{doc}::Run')]
        self.assertEqual(source_station_displays(groups, identities), {})
        self.assertEqual(source_station_displays([(*groups[0][:2], 'LineA::Other::Run')], identities), {})

    def test_end_comment_is_context_and_never_start_description(self):
        self.record()
        self.source_edges(self.run, [(10, 30)])
        self.write_csv(self.run / 'source_stage_candidates.csv', [self.comment(30, 40, '30-机器人取料', 'T9')])
        _, info = self.prepare()
        self.assertEqual(info['description'], '')
        self.assertTrue((self.run / 'batches_desc/batch_001.input.json').exists())

    def test_generated_reset_hypothesis_is_not_a_source_comment(self):
        self.record()
        self.write_csv(self.run / 'indexed_candidates.csv', [{
            '开始标签': 'Step[1]=10', '结束标签': 'Step[1]=0',
            '动作描述': '0状态已出现，是否由当前状态跳转尚待验证',
            '状态': 'reset_hypothesis', '证据POU': 'Program::Step', '证据节点': 'observed_states'}])
        _, info = self.prepare()
        self.assertEqual(info['description'], '')
        self.assertEqual(info['start_notes'], [])

    def test_conflicting_start_comments_are_sent_to_llm(self):
        self.record(10, 99)
        self.source_edges(self.run, [(10, 20), (10, 30)])
        self.write_csv(self.run / 'source_stage_candidates.csv', [
            self.comment(10, 20, '10-气缸伸出'), self.comment(10, 30, '10-夹爪夹紧', 'T2')])
        _, info = self.prepare()
        self.assertEqual(info['description'], '')
        texts = {e['data']['text'] for e in info['description_context']['evidence'] if e['category'] == 'start_comment'}
        self.assertEqual(texts, {'气缸伸出', '夹爪夹紧'})

    def analogy_fixture(self):
        self.record(10, 20)
        self.source_edges(self.run, [(10, 20)])
        command = self.command(10, 1, 'CustomCycle', 'Device=EM01.Cy_Up2;Enable=RunSignal')
        command['guard_complete'] = False
        effect = self.command(0, 2, 'coil_set', 'Device.CycleRequest')
        effect.update(scope='CustomCycle::body', start='', guard='Enable')
        self.write_csv(self.run / 'native_command_evidence.csv', [command, effect])
        (self.run / 'variable_documentation.json').write_text(json.dumps({'all': {
            'EM01.Cy_Up1': '上料气缸伸出位置', 'EM02.Cy_Up1': '伺服电机'}}, ensure_ascii=False))
        return self.prepare()[1]

    def write_result(self, row, refs, action='启动循环', *, source_ref='', subject_symbol=''):
        result = {'basis': row['basis'], 'ok': True, 'kind': '机械动作',
                  'actions': [{'subject': '气缸', 'action': action, 'evidence_refs': refs,
                               'source_ref': source_ref, 'subject_symbol': subject_symbol}],
                  'reasoning': '本步调用绑定该变量，同命名族文档支持气缸类别，方向未知'}
        data = {'batch_id': 1, 'descs': {'Step[1]=10|||Step[1]=20': result}}
        (self.run / 'batches_desc/batch_001.result.json').write_text(json.dumps(data, ensure_ascii=False))
        return result, data

    def test_same_family_memory_supports_class_but_not_direction(self):
        info = self.analogy_fixture()
        items = info['description_context']['evidence']
        command = next(e for e in items if e['category'] == 'current_command')
        effect = next(e for e in items if e['category'] == 'bound_write')
        analogies = [e for e in items if e['category'] == 'naming_analogy']
        self.assertEqual(len(analogies), 1)
        self.assertEqual(analogies[0]['data']['example_symbol'], 'EM01.Cy_Up1')
        self.assertIn('上料气缸伸出位置', analogies[0]['data']['example_text'])
        self.assertFalse(analogies[0]['local'])
        refs = [command['id'], analogies[0]['id']]
        result, data = self.write_result(info, refs, source_ref=effect['id'], subject_symbol='EM01.Cy_Up2')
        stats = {}
        final = cmd_validate(self.run, stats=stats)
        self.assertEqual(stats['b_candidates'], 1)
        result_row, = self.read_csv(final)
        self.assertEqual(result_row['证据等级'], '同类命名推断')
        self.assertIn(effect['id'], result_row['证据'])
        self.assertIn('命名类比', result_row['证据'])
        self.assertNotIn('方向未知', result_row['证据'])
        ws = workbook(str(self.run / 'trace_collection_pairs.csv'), run=str(self.run), sheet='trace步序采集表')
        self.assertEqual(Path(ws['out']).name, '包mylar_trace步序采集表.xlsx')
        from openpyxl import load_workbook
        book = load_workbook(ws['out'], read_only=True)
        sheet = book.active
        self.assertEqual(sheet['D4'].value, 'LLM')
        self.assertEqual(sheet['D4'].font.color.rgb[-6:], 'FF0000')
        self.assertEqual(sheet['J4'].value, result_row['证据'])
        remarks = [str(row[0]) for row in sheet.iter_rows(min_col=9, max_col=9, values_only=True)]
        book.close()
        self.assertTrue(any('同类命名推断' in text and '方向未知' in text for text in remarks))
        result['actions'][0].update(action='伸出', source_ref=analogies[0]['id'])
        (self.run / 'batches_desc/batch_001.result.json').write_text(json.dumps(data, ensure_ascii=False))
        cmd_validate(self.run, stats=stats)
        self.assertEqual(stats['b_candidates'], 0)
        rejected, = self.read_csv(final)
        self.assertEqual((rejected['动作描述'], rejected['证据']), ('', ''))
        self.assertIn('本步', self.read_csv(self.run / 'action_desc_review.csv')[0]['原因'])
        result['actions'][0]['source_ref'] = command['id']
        self.assertIn('缺少本步动作效果', validate_context_result(result, info['description_context'])[1])

    def test_legacy_evidence_lists_references_without_assigning_them_to_a_clause(self):
        from trace_description_report import action_evidence_text

        mapping = {'id': 'legacy-name', 'category': 'annotated_mapping', 'data': {
            'target': 'Actuator[2]', 'example_symbol': 'Actuator[2]', 'mechanism_classes': ['气缸'],
        }}
        info = {'description_context': {'evidence': [mapping]}}
        evidence = action_evidence_text(info, {'refs': [mapping['id']]})
        self.assertEqual(evidence, '同对象注释，Actuator[2] 参考 Actuator[2]「气缸」〔legacy-name〕')

    def test_source_change_invalidates_old_context_results(self):
        info = self.analogy_fixture()
        refs = [e['id'] for e in info['description_context']['evidence'] if e['category'] in ('current_command', 'naming_analogy')]
        self.write_result(info, refs)
        before = (self.run / 'batches_desc/batch_001.result.json').read_bytes()
        path = self.run / 'variable_documentation.json'
        path.write_text(json.dumps({'all': {'EM01.Cy_Up1': '夹爪气缸'}}))
        _, after = self.prepare()
        self.assertNotEqual(info['basis'], after['basis'])
        self.assertEqual(before, (self.run / 'batches_desc/batch_001.result.json').read_bytes())
        stats = {}
        cmd_validate(self.run, stats=stats)
        self.assertEqual(stats['b_candidates'], 0)
        self.assertIn('版本', self.read_csv(self.run / 'action_desc_review.csv')[0]['原因'])

    def test_annotated_call_teaches_same_scope_object_family(self):
        self.record(10, 20)
        self.source_edges(self.run, [(5, 10), (10, 20)])
        known = self.command(5, 1, 'oneCyControl', 'CY=EM01.Cy_Up1;Enable=RunSignal')
        known['comment'] = '5-升降气缸伸出'
        unknown = self.command(10, 2, 'oneCyControl', 'CY=EM01.Cy_Up2;Enable=RunSignal')
        effect = self.command(0, 3, 'coil_set', 'CY.AutoStrOut')
        effect.update(scope='OneCyControl::body', start='', guard='Enable')
        self.write_csv(self.run / 'native_command_evidence.csv', [known, unknown, effect])
        _, info = self.prepare()
        self.assertEqual(info['description'], '')
        examples = [e for e in info['description_context']['evidence'] if e['category'] == 'naming_analogy']
        self.assertTrue(any(e['data']['example_symbol'] == 'EM01.Cy_Up1' and
                            e['data']['example_text'] == known['comment'] for e in examples))
        self.assertTrue(all(not e['local'] for e in examples))

    def test_all_unannotated_rows_are_batched_even_without_source_match(self):
        self.record()
        _, info = self.prepare()
        self.assertEqual(info['relation'], 'unresolved')
        batch = json.loads((self.run / 'batches_desc/batch_001.input.json').read_text())
        self.assertEqual(len(batch['rows']), 1)
        refs = [info['description_context']['evidence'][0]['id']]
        self.write_result(info, refs, '焊接')
        # Correct the result key for this observed interval.
        path = self.run / 'batches_desc/batch_001.result.json'
        result = json.loads(path.read_text());result['descs']['Step[1]=10|||Step[1]=30'] = result['descs'].pop('Step[1]=10|||Step[1]=20')
        path.write_text(json.dumps(result))
        stats = {}
        cmd_validate(self.run, stats=stats)
        self.assertEqual(stats['b_tier_pending_rows'], 1)
        self.assertEqual(stats['b_tier_invalid_rows'], 1)
        self.assertEqual(stats['b_candidates'], 0)
        result['descs']['Step[1]=10|||Step[1]=30'] = {
            'basis': info['basis'], 'ok': False, 'reason': '采样只提供状态对，缺少源码指令'}
        path.write_text(json.dumps(result, ensure_ascii=False))
        cmd_validate(self.run, stats=stats)
        self.assertEqual((stats['b_tier_pending_rows'], stats['unresolved_rows']), (0, 1))

    def test_bound_effects_keep_both_objects_and_select_literal_direction(self):
        self.record(10, 30)
        self.source_edges(self.run, [(10, 0), (0, 30)])
        call = self.command(10, 1, 'ControlTwo', 'CY_1=Cylinder[3];CY_2=Cylinder[4];D1=0;D2=1;Mode=Manual')
        call['comment'] = '5-松开AB夹子'
        rows = [call]
        for index, label in [(3, '下料_A夹子气缸'), (4, '下料_B夹子气缸')]:
            binding = self.command(0, index, 'BindDevice', f'CY=Cylinder[{index}]')
            binding.update(scope='Program::Bindings', start='', comment=label)
            rows.append(binding)
        rows.append(dict(rows[1], scope='OtherProgram::Bindings', comment='其他工位气缸'))
        for slot in (1, 2):
            for member, term in [('AutoAnastole', f'NOT D{slot}'), ('AutoStrOut', f'D{slot}')]:
                write = self.command(0, len(rows), 'coil_set', f'CY_{slot}.{member}')
                write.update(scope='ControlTwo::body', start='', guard=f'{term} AND NOT Mode AND EN')
                rows.append(write)
        self.write_csv(self.run / 'native_command_evidence.csv', rows)
        _, info = self.prepare()
        self.assertEqual(info['relation'], 'path_via_zero_candidate')
        self.assertEqual([e['to'] for e in info['candidate_path']], [0, 30])
        context = info['description_context']
        writes = [e['data'] for e in context['evidence'] if e['category'] == 'bound_write']
        self.assertEqual({e['target'] for e in writes}, {'Cylinder[3].AutoAnastole', 'Cylinder[4].AutoStrOut'})
        self.assertTrue(all('NOT (Manual)' in e['bound_guard'] for e in writes))
        anchors = context['required_actions']
        result = {'ok': True, 'basis': info['basis'], 'kind': '机械动作', 'reasoning': '按绑定后的条件写入'}
        result['actions'] = [{'action_ref': anchors[0]}]
        self.assertIn('B侧夹子气缸', validate_context_result(result, context)[1])
        result['actions'].append({'action_ref': anchors[1]})
        candidate, issue = validate_context_result(result, context)
        self.assertFalse(issue)
        self.assertEqual(candidate['desc'], '向下料A侧夹子气缸发出回原位请求，向下料B侧夹子气缸发出工位侧请求')
        facts = {item['id']: item for item in context['evidence']}
        targets = [{facts[ref]['data']['target'] for ref in refs if facts[ref]['category'] == 'bound_write'}
                   for _, refs in candidate['clause_evidence']]
        self.assertEqual(targets, [{'Cylinder[3].AutoAnastole'}, {'Cylinder[4].AutoStrOut'}])
        call_ref = next(e['id'] for e in context['evidence'] if e['category'] == 'current_command')
        result['actions'].append({'subject': '下料A侧夹子气缸', 'action': '松开', 'source_ref': call_ref,
                                  'subject_symbol': 'Cylinder[3]'})
        self.assertIn('已由动作锚点覆盖', validate_context_result(result, context)[1])
        result['actions'][-1]['source_ref'] = next(
            e['id'] for e in context['evidence'] if e['category'] == 'candidate_path')
        self.assertIn('仅可作上下文', validate_context_result(result, context)[1])

    def test_timeout_monitoring_evidence_does_not_support_a_fixed_dwell(self):
        self.record(10, 20)
        self.source_edges(self.run, [(10, 20)])
        timer = self.command(10, 1, 'TON', 'PT=T#5S')
        coil = self.command(10, 1, 'coil_set', 'Alarm[1]')
        coil.update(node='2', path='1')
        self.write_csv(self.run / 'native_command_evidence.csv', [timer, coil])
        _, info = self.prepare()
        context = info['description_context']
        monitor, = [e for e in context['evidence'] if e['category'] == 'timer_alarm']
        result = {'ok': True, 'basis': info['basis'], 'kind': '内部辅助', 'reasoning': '定时器输出接报警线圈',
                  'actions': [{'subject': '取料条件', 'action': '超时监控',
                               'source_ref': monitor['id'], 'subject_symbol': 'Alarm[1]'}]}
        self.assertEqual(validate_context_result(result, context)[0]['desc'], '取料条件超时监控')
        result['actions'][0].update(action='等待', target='5秒')
        self.assertIn('5秒', validate_context_result(result, context)[1])

    def test_unfamiliar_device_action_and_unit_use_bound_source_evidence(self):
        self.record(10, 20)
        self.source_edges(self.run, [(10, 20)])
        call = self.command(10, 1, 'DepositCycle', 'Substrate=Cell.靶位2;FilmNm=REAL#25.5;Enable=RunSignal')
        effect = self.command(0, 2, 'coil_set', 'Substrate.SputterRequest')
        effect.update(scope='DepositCycle::body', start='', guard='Enable')
        self.write_csv(self.run / 'native_command_evidence.csv', [call, effect])
        (self.run / 'variable_documentation.json').write_text(
            json.dumps({'all': {'Cell.靶位2': '氮化硅靶'}}, ensure_ascii=False))
        _, info = self.prepare()
        context = info['description_context']
        self.assertEqual(context['required_actions'], [])
        write, = [e for e in context['evidence'] if e['category'] == 'bound_write']
        mapping, = [e for e in context['evidence'] if e['category'] == 'exact_mapping']
        clause = {'subject': '氮化硅靶', 'action': '磁控溅射镀膜指令下发', 'target': '，膜厚设定25.5纳米',
                  'source_ref': write['id'], 'subject_symbol': 'Cell.靶位2',
                  'target_parameter': 'FilmNm', 'evidence_refs': [mapping['id']]}
        result = {'ok': True, 'basis': info['basis'], 'kind': '薄膜沉积', 'actions': [clause],
                  'reasoning': '实参绑定靶位2，SputterRequest置位，FilmNm设为25.5'}
        path = self.run / 'batches_desc/batch_001.result.json'
        path.write_text(json.dumps({'batch_id': 1, 'descs': {'Step[1]=10|||Step[1]=20': result}},
                                   ensure_ascii=False))
        final = cmd_validate(self.run)
        row, = self.read_csv(final)
        self.assertEqual(row['动作描述'], '氮化硅靶磁控溅射镀膜指令下发，膜厚设定25.5纳米')
        self.assertEqual(row['描述状态'], 'LLM推断待核验')
        self.assertEqual(row['行性质'], '薄膜沉积')
        for key, value, issue in [
            ('subject_symbol', 'Cell.靶位1', '不匹配'),
            ('source_ref', mapping['id'], '本步'),
            ('target', '至26纳米', '缺少本步'),
            ('action', '', '对象和动作'),
        ]:
            changed = {**result, 'actions': [{**clause, key: value}]}
            self.assertIn(issue, validate_context_result(changed, context)[1])

    def test_context_includes_guarded_call_and_shared_symbol_binding(self):
        self.record(10, 20)
        self.source_edges(self.run, [(10, 20)])
        self.write_csv(self.run / 'native_command_evidence.csv', [self.command(10, 1, 'CustomCycle', 'Device=EM01.Cy_Up2')])
        self.write_csv(self.run / 'indexed_events.csv', [
            {'source_unit': 'Program::Step', 'network': '1', 'guard': 'Step[1]=10 AND Ready',
             'command': 'CustomCycle', 'arguments': 'Device=EM01.Cy_Up2', 'feedback': 'Device.Done', 'next_state': '20'},
            {'source_unit': 'Helper::Feedback', 'network': '2', 'guard': 'EM01.Cy_Up2.Done',
             'command': 'MOVE', 'arguments': 'EM01.Cy_Up2', 'feedback': '', 'next_state': 'Signal=1'},
        ])
        _, info = self.prepare()
        kinds = {e['category'] for e in info['description_context']['evidence']}
        self.assertTrue({'guarded_call', 'related_call', 'direct_control'} <= kinds)


class ProcessNamingTests(unittest.TestCase):
    def test_chinese_mixed_name_folder_and_timestamp(self):
        source = r'E:\projects\source\包mylar\L8A1-Mylar PLC程序_202608251035.xml'
        self.assertEqual(infer_proc_name(source, 'outputs/L8A1-Mylar_trace'), '包mylar')
        self.assertEqual(infer_proc_name('source/包mylar.xml'), '包mylar')
        self.assertEqual(infer_proc_name(None, 'outputs/包mylar_trace'), '包mylar')
        self.assertEqual(infer_proc_name('source/1.JM_CATL_厦门时代_转接片_20260813.xml'), '转接片')
        self.assertEqual(infer_proc_name(source, proc_name='包mylar调试'), '包mylar调试')
        with self.assertRaises(ValueError):
            infer_proc_name(source, proc_name='../包mylar')
