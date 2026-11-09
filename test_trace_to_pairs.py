import csv
import json
import tempfile
import unittest
from pathlib import Path

from trace_to_pairs import generate, parse_omron_trace


class SysmacRecordingTests(unittest.TestCase):
    FIRST = '20260101080000'
    SECOND = '20260101090000'
    EPOCH = 1_700_000_000_000_000_000

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.trace = self.root / 'trace'
        self.trace.mkdir()

    def write_part(self, filename, epoch, rows, *, channels=('Step[1]',),
                   period_ms=12, rounded=False):
        with (self.trace / filename).open('w', encoding='utf-8-sig', newline='') as file:
            writer = csv.writer(file)
            writer.writerows([
                ['TraceType', 'Continuous'], ['SampleTimingType', 'interval'],
                ['StartRawTime', '1.7E+18' if rounded else epoch],
                ['Index', 'RawTime', *(name + ':INT' for name in channels)],
            ])
            for idx, *values in rows:
                raw = '1.7E+18' if rounded else epoch + idx * period_ms * 1_000_000
                writer.writerow([idx, raw, *values])

    def read_csv(self, path):
        with open(path, encoding='utf-8-sig', newline='') as file:
            return list(csv.DictReader(file))

    def write_csv(self, path, rows):
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def source_edges(self, run, edges):
        self.write_csv(run / 'source_transition_edges.csv', [
            {
                'id': f'T{i}', 'scope': 'Program::Step', 'binding_scope': '',
                'body': f'{i};line:{i};node:{i}', 'variable': 'Step[1]',
                'from': start, 'to': end, 'guard': f'Step[1]={start} AND Ready',
                'guard_complete': True, 'evidence': 'omron_native_wired_guard', 'path': str(i),
            }
            for i, (start, end) in enumerate(edges, 1)
        ])

    def command(self, start, network, command, arguments):
        return {
            'scope': 'Program::Step', 'network': network, 'source_line': '100', 'node': str(network),
            'start': f'Step[1]={start}', 'command': command, 'arguments': arguments,
            'guard': f'Step[1]={start}', 'guard_complete': True, 'issue': '', 'path': '', 'comment': '',
        }

    def test_filename_groups_parts_and_estimates_each_recording_separately(self):
        later = self.EPOCH + 3_600_000_000_000
        self.write_part(self.SECOND + '_00002.csv', later, [(2, 100), (3, 100)], rounded=True)
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 10), (1, 10)])
        self.write_part(self.SECOND + '_00001.csv', later, [(0, 90), (1, 90)], period_ms=20)
        self.write_part(self.FIRST + '_00002.csv', self.EPOCH, [(2, 20), (3, 20)], rounded=True)

        stats = generate(self.trace, self.root / 'run', proc_name='test')
        details = stats['trace_input']['recording_details']
        self.assertEqual([r['recording_id'] for r in details], [self.FIRST, self.SECOND])
        self.assertEqual([len(r['files']) for r in details], [2, 2])
        self.assertEqual([r['sample_interval_ms'] for r in details], [12, 20])
        rows = self.read_csv(stats['outputs']['pairs_csv'])
        self.assertEqual({(r['开始标签'], r['结束标签'], r['中位数时长ms']) for r in rows},
                         {('Step[1]=10', 'Step[1]=20', '24'),
                          ('Step[1]=90', 'Step[1]=100', '40')})

    def test_same_sample_index_keeps_distinct_dwell_and_timing_basis(self):
        rows = [(0, 10), (1, 20), (2, 20)]
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, rows)
        self.write_part(self.SECOND + '_00001.csv', self.EPOCH + 3_600_000_000_000,
                        rows, rounded=True)
        with self.assertRaisesRegex(ValueError, self.SECOND + '.*RawTime'):
            parse_omron_trace(self.trace)
        stats = generate(self.trace, self.root / 'run', proc_name='test', sample_interval_ms=12)
        row, = self.read_csv(stats['outputs']['pairs_csv'])
        self.assertEqual(tuple(row[k] for k in ('发生次数', '最小时长ms', '中位数时长ms',
                                                 '最大时长ms', '估算时长次数', '所属周期数')),
                         ('2', '12', '12', '12', '1', '2'))
        self.assertEqual(stats['cycles_detected'], 0)
        jumps = self.read_csv(stats['outputs']['jumps'])
        self.assertEqual([(r['采集ID'], r['样本索引']) for r in jumps],
                         [(self.FIRST, '1'), (self.SECOND, '1')])

    def test_channel_filter_can_select_a_channel_missing_from_first_recording(self):
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 1), (1, 2)])
        self.write_part(self.SECOND + '_00001.csv', self.EPOCH + 3_600_000_000_000,
                        [(0, 1, 30), (1, 2, 40)], channels=('Step[1]', 'Step[2]'))
        variables, _ = parse_omron_trace(self.trace)
        self.assertEqual(set(variables), {'Step[1]', 'Step[2]'})
        variables, audit = parse_omron_trace(self.trace, matches=lambda name: name == 'Step[2]')
        self.assertEqual(set(variables), {'Step[2]'})
        self.assertEqual(variables['Step[2]']['jumps'], [(1, 30, 40, 12)])
        self.assertEqual([r['channels_selected'] for r in audit['recording_details']], [0, 1])

    def test_missing_indices_are_audited_and_do_not_create_jumps(self):
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 10), (1, 10)])
        self.write_part(self.FIRST + '_00003.csv', self.EPOCH, [(4, 20), (5, 30)])
        variables, audit = parse_omron_trace(self.trace)
        self.assertEqual(variables['Step[1]']['jumps'], [(5, 20, 30, 60)])
        self.assertEqual(audit['index_gaps'],
                         [{'after_index': 1, 'before_index': 4, 'recording_id': self.FIRST}])

    def test_renamed_parts_attach_only_to_one_compatible_exact_start(self):
        self.write_part('original.csv', self.EPOCH, [(0, 10), (1, 10)])
        self.write_part('renamed.csv', self.EPOCH, [(2, 20), (3, 20)], rounded=True)
        variables, audit = parse_omron_trace(self.trace)
        self.assertEqual(audit['recording_details'][0]['grouping_source'], 'start_raw_time')
        self.assertEqual(variables['Step[1]']['jumps'], [(2, 10, 20, 24)])

        self.write_part('another.csv', self.EPOCH + 3_600_000_000_000, [(0, 30), (1, 30)])
        with self.assertRaisesRegex(ValueError, 'renamed.csv.*无法唯一确定所属采集'):
            parse_omron_trace(self.trace)

    def test_named_recording_rejects_conflicting_sample_ranges(self):
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 10), (1, 10)])
        self.write_part(self.FIRST + '_00002.csv', self.EPOCH, [(1, 20), (2, 20)])
        with self.assertRaisesRegex(ValueError, 'Index 重叠'):
            parse_omron_trace(self.trace)

    def test_source_path_and_comment_preserve_the_observed_jump(self):
        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 0), (1, 10), (2, 30)])
        run = self.root / 'run'
        run.mkdir()
        self.source_edges(run, [(0, 10), (10, 20), (20, 30)])
        self.write_csv(run / 'source_stage_candidates.csv', [{
            '开始标签': 'Step[1]=0', '结束标签': 'Step[1]=10',
            '证据POU': 'Program::Step', '证据节点': 'T1', '网络序号': '1',
            '源码行号': '1', '动作描述': '步0-模式跳转', '状态': 'native_wired_transition',
        }])
        self.write_csv(run / 'native_command_evidence.csv', [self.command(0, 9, 'coil_reset', 'PPM')])

        stats = generate(self.trace, run, proc_name='test')
        evidence = json.loads((run / 'trace_source_evidence.json').read_text(encoding='utf-8'))['pairs']
        self.assertEqual(stats['source_relations'], {'direct': 1, 'path_candidate': 1})
        direct = evidence['Step[1]=0|||Step[1]=10']
        self.assertEqual(direct['description'], '模式跳转')
        self.assertEqual(direct['b_evidence'], [])
        self.assertEqual(direct['start_commands'][0]['operation'], '复位')
        skip = evidence['Step[1]=10|||Step[1]=30']
        self.assertEqual([edge['to'] for edge in skip['candidate_path']], [20, 30])
        rows = self.read_csv(stats['outputs']['pairs_csv'])
        self.assertEqual([(r['开始标签'], r['结束标签'], r['中位数时长ms']) for r in rows],
                         [('Step[1]=0', 'Step[1]=10', '12'), ('Step[1]=10', 'Step[1]=30', '12')])

    def test_trace_descriptions_require_current_command_evidence_and_all_rows(self):
        from action_describe import cmd_a, cmd_build, cmd_validate

        self.write_part(self.FIRST + '_00001.csv', self.EPOCH, [(0, 0), (1, 10), (2, 20), (3, 30)])
        run = self.root / 'run'
        run.mkdir()
        self.source_edges(run, [(10, 20), (20, 30)])
        self.write_csv(run / 'native_command_evidence.csv', [
            self.command(10, 1, 'AxisMove', '_Axis=Axis1;_Position=3'),
            self.command(20, 2, 'coil_reset', 'PPM'),
        ])
        generate(self.trace, run, proc_name='test')
        cmd_a(run)
        self.assertEqual(cmd_build(run, batch_size=1), 3)
        batch_dir = run / 'batches_desc'
        batch = json.loads((batch_dir / 'batch_002.input.json').read_text(encoding='utf-8'))
        self.assertEqual(batch['schema'], 'trace-description-context-v3')
        row, = batch['rows']
        command = next(e for e in row['context']['evidence'] if e['category'] == 'current_command')
        anchor, = row['context']['required_actions']
        result = {'basis': row['basis'], 'ok': True, 'kind': '机械动作',
                  'actions': [{'subject': '轴', 'action': '动作', 'evidence_refs': [command['id']]}],
                  'reasoning': '测试证据约束'}
        path = batch_dir / 'batch_002.result.json'
        data = {'batch_id': 2, 'descs': {row['id']: result}}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        stats = {}
        final = cmd_validate(run, stats=stats)
        self.assertEqual((stats['b_candidates'], stats['unresolved_rows'], stats['b_tier_pending_rows']),
                         (0, 0, 3))
        self.assertEqual(stats['b_tier_invalid_rows'], 1)
        self.assertEqual(stats['missing_b_tier_batches'], ['batch_001.result.json', 'batch_003.result.json'])
        self.assertTrue(all(not r['动作描述'] for r in self.read_csv(final)))
        self.assertNotIn('nan', (batch_dir / 'batch_002.input.json').read_text(encoding='utf-8'))

        result['actions'] = [{'action_ref': anchor}]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        final = cmd_validate(run, stats=stats)
        self.assertEqual(stats['b_candidates'], 1)
        described, = [r for r in self.read_csv(final) if r['动作描述']]
        self.assertEqual(described['描述来源'], 'B档(上下文推断)')
        self.assertEqual(described['动作描述'], '1号轴定位至3号位')
        self.assertEqual(set(described['证据引用'].split('; ')), {anchor, command['id']})

        (batch_dir / 'batch_003.result.json').write_text('{"batch_id":3,"descs":{}}', encoding='utf-8')
        cmd_validate(run, stats=stats)
        self.assertEqual(stats['b_tier_result_count'], 2)
        self.assertEqual(stats['b_tier_pending_rows'], 2)

        result['actions'].append({'subject': '轴', 'action': '焊接定位', 'evidence_refs': [command['id']]})
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        cmd_validate(run, stats=stats)
        self.assertEqual(stats['b_candidates'], 0)

        result['actions'] = [{'action_ref': anchor}]
        result['basis'] = 'old-source'
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        cmd_validate(run, stats=stats)
        self.assertEqual(stats['b_candidates'], 0)
