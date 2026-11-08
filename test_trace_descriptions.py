import json
import tempfile
import unittest
from pathlib import Path

import test_trace_to_pairs as fixtures
from action_describe import cmd_a, cmd_build, cmd_validate
from gen_workstation_xlsx import generate as workbook
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
        self.write_csv(self.run / 'native_command_evidence.csv', [command])
        (self.run / 'variable_documentation.json').write_text(json.dumps({'all': {
            'EM01.Cy_Up1': '上料气缸伸出位置', 'EM02.Cy_Up1': '伺服电机'}}, ensure_ascii=False))
        return self.prepare()[1]

    def write_result(self, row, refs, desc='气缸动作'):
        result = {'basis': row['basis'], 'ok': True, 'desc': desc, 'kind': '机械动作',
                  'evidence_refs': refs, 'reasoning': '本步调用绑定该变量，同命名族文档支持气缸类别，方向未知'}
        data = {'batch_id': 1, 'descs': {'Step[1]=10|||Step[1]=20': result}}
        (self.run / 'batches_desc/batch_001.result.json').write_text(json.dumps(data, ensure_ascii=False))
        return result, data

    def test_same_family_memory_supports_class_but_not_direction(self):
        info = self.analogy_fixture()
        items = info['description_context']['evidence']
        command = next(e for e in items if e['category'] == 'current_command')
        analogies = [e for e in items if e['category'] == 'naming_analogy']
        self.assertEqual(len(analogies), 1)
        self.assertEqual(analogies[0]['data']['example_symbol'], 'EM01.Cy_Up1')
        self.assertNotIn('伸出', json.dumps(analogies, ensure_ascii=False))
        refs = [command['id'], analogies[0]['id']]
        result, data = self.write_result(info, refs)
        stats = {}
        final = cmd_validate(self.run, stats=stats)
        self.assertEqual(stats['b_candidates'], 1)
        self.assertEqual(self.read_csv(final)[0]['证据等级'], '同类命名推断')
        ws = workbook(str(self.run / 'trace_collection_pairs.csv'), run=str(self.run), sheet='trace步序采集表')
        self.assertEqual(Path(ws['out']).name, '包mylar_trace步序采集表.xlsx')
        from openpyxl import load_workbook
        book = load_workbook(ws['out'], read_only=True)
        remarks = [str(row[0]) for row in book.active.iter_rows(min_col=8, max_col=8, values_only=True)]
        book.close()
        self.assertTrue(any('同类命名推断' in text and '方向未知' in text for text in remarks))
        result['desc'] = '气缸伸出'
        (self.run / 'batches_desc/batch_001.result.json').write_text(json.dumps(data, ensure_ascii=False))
        cmd_validate(self.run, stats=stats)
        self.assertEqual(stats['b_candidates'], 0)
        self.assertIn('伸出', self.read_csv(self.run / 'action_desc_review.csv')[0]['原因'])

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
        self.write_csv(self.run / 'native_command_evidence.csv', [known, unknown])
        _, info = self.prepare()
        self.assertEqual(info['description'], '')
        examples = [e for e in info['description_context']['evidence'] if e['category'] == 'naming_analogy']
        self.assertTrue(any(e['data']['example_symbol'] == 'EM01.Cy_Up1' and
                            e['data']['mechanism_classes'] == ['气缸'] for e in examples))
        self.assertTrue(all('伸出' not in json.dumps(e, ensure_ascii=False) for e in examples))

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
        self.assertEqual(stats['b_tier_pending_rows'], 0)
        self.assertEqual(stats['b_candidates'], 0)

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
