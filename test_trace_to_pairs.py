import csv
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
