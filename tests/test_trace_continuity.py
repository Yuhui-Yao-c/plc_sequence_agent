import csv
import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from openpyxl import load_workbook

from plc_trace.description.action_describe import cmd_a, cmd_build, cmd_validate
from plc_trace.output.gen_workstation_xlsx import generate as workbook
from plc_trace.trace.sample_continuity import clock_breaks
from plc_trace.trace.trace_to_pairs import generate


class SamplingContinuityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.run = self.root / 'run'

    def native(self, *records, microseconds=False):
        path = self.root / 'observations.trace'
        root = ET.Element('Trace')
        flag = ET.SubElement(ET.SubElement(root, 'TraceConfiguration'), 'Single', Name='MicroSeconds')
        flag.text = str(microseconds)
        data = ET.SubElement(root, 'TraceData')
        for channels in records:
            rec = ET.SubElement(data, 'TraceRecord')
            for name, (times, values) in channels.items():
                channel = ET.SubElement(rec, 'TraceVariable', VarName=name)
                ET.SubElement(channel, 'Values').text = ','.join('' if v is None else str(v) for v in values) + ','
                ET.SubElement(channel, 'Timestamps').text = ','.join(map(str, times)) + ','
        ET.ElementTree(root).write(path, encoding='utf-16')
        return path

    def rows(self, stats, key='pairs_csv'):
        with open(stats['outputs'][key], newline='', encoding='utf-8-sig') as stream:
            return list(csv.DictReader(stream))

    def test_shared_range_uses_all_channel_clocks_before_filter(self):
        times = [*range(7), *range(100, 107)]
        values = [0, 1, 1, 2, 2, 2, 2, 9, 9, 1, 1, 2, 2, 2]
        channels = {'A': (times, values), 'B': ([*range(1, 8), *range(99, 106)], values)}
        stats = generate(self.native(channels), self.run, pattern='A')
        info, = json.loads(Path(stats['outputs']['sampling_gaps']).read_text())['recordings']
        self.assertEqual(info['common_gaps'], [{'left_ms': 7, 'right_ms': 99, 'duration_ms': 92}])
        self.assertEqual([(g['channel'], g['left_value'], g['right_value']) for g in info['channel_gaps']],
                         [('A', 2, 9), ('B', 2, 9)])
        pairs = {(r['开始标签'], r['结束标签']): r for r in self.rows(stats)}
        self.assertNotIn(('A=2', 'A=9'), pairs)
        self.assertEqual(pairs['A=0', 'A=1']['采集首段未计时次数'], '1')
        self.assertEqual(pairs['A=9', 'A=1']['缺口后未计时次数'], '1')
        self.assertEqual((pairs['A=1', 'A=2']['有效计时次数'], pairs['A=1', 'A=2']['中位数时长ms']), ('2', '2'))
        self.assertEqual((stats['total_jumps'], stats['valid_dwell_count']), (4, 2))
        channels['C'] = (range(107), [0] + [1] * 104 + [2, 2])
        stats = generate(self.native(channels), self.run)
        self.assertEqual(stats['common_gap_count'], 0)
        c = next(r for r in self.rows(stats) if r['开始标签'] == 'C=1')
        self.assertEqual(c['中位数时长ms'], '104')

    def test_constant_across_gap_is_a_new_span_and_zero_dwell_is_numeric(self):
        path = self.native({'A': ([0, 100, 200, 300, 400, 500, 600, 10000, 10100, 10200, 10200, 10300],
                                  [0, 1, 2, 1, 1, 1, 1, 1, 2, 3, 4, 2])}, microseconds=True)
        stats = generate(path, self.run)
        pairs = {(r['开始标签'], r['结束标签']): r for r in self.rows(stats)}
        pair = pairs['A=1', 'A=2']
        self.assertEqual(tuple(pair[k] for k in ('发生次数', '有效计时次数', '缺口后未计时次数', '中位数时长ms')),
                         ('2', '1', '1', '0.1'))
        zero = pairs['A=3', 'A=4']
        self.assertEqual(float(zero['中位数时长ms']), 0)
        self.assertEqual(zero['有效计时次数'], '1')
        self.assertEqual((stats['total_jumps'], stats['valid_dwell_count'], stats['partial_dwell_count']),
                         (7, 5, 2))

    def test_independent_native_records_with_clock_reset_and_missing_cell(self):
        path = self.native({'A': ([100, 101, 102, 103, 104], [0, 1, None, 2, 3])},
                           {'A': ([40, 41, 42], [6, 1, 2])})
        stats = generate(path, self.run)
        rows = self.rows(stats, 'jumps')
        self.assertEqual([(r['采集ID'], r['样本索引'], r['计时状态']) for r in rows],
                         [('1', '1', '采集首段'), ('1', '4', '缺口后首段'),
                          ('2', '1', '采集首段'), ('2', '2', '完整')])
        self.assertEqual(stats['valid_dwell_count'], 1)
        self.assertEqual(stats['total_jumps'], 4)
        self.assertEqual(stats['channel_gap_count'], 1)
        self.assertNotIn(('3', '6'), {(r['from值'], r['to值']) for r in rows})

    def test_only_cross_gap_change_renders_audit_even_with_empty_main_table(self):
        times = [*range(7), *range(100, 107)]
        stats = generate(self.native({'A': (times, [1] * 7 + [2] * 7)}), self.run)
        self.assertEqual((stats['unique_pairs'], stats['total_jumps'], stats['common_gap_count']), (0, 0, 1))
        cmd_a(self.run)
        cmd_build(self.run)
        cmd_validate(self.run)
        result = workbook(stats['outputs']['pairs_csv'], run=str(self.run), sheet='trace步序采集表')
        with open(result['out'], 'rb') as stream:
            book = load_workbook(stream)
        self.addCleanup(book.close)
        self.assertEqual(result['rows'], 0)
        self.assertEqual(book.active.max_row, 2)
        detail = book['采样缺口']
        self.assertEqual([detail.cell(4, c).value for c in (3, 4, 5, 6, 7, 8, 9)], ['A', 6, 6, 1, 7, 100, 2])

    def test_sysmac_counter_gaps_raw_time_gaps_local_blanks_and_event_clock(self):
        folder = self.root / 'capture'
        folder.mkdir()
        path = folder / '20260101080000_00001.csv'
        times = [*range(8), *range(100, 108)]
        states = [0, 1, 2, 3, 3, 3, 3, 3, 9, 10, 20, 20, 30, 40, 40, 40]
        def write(mode):
            with path.open('w', newline='', encoding='utf-8') as stream:
                w = csv.writer(stream)
                w.writerows([['TraceType', 'Continuous'], ['SampleTimingType', mode],
                             ['Index', 'RawTime', 'A:INT', 'B:INT']])
                for i, (t, v) in enumerate(zip(times, states)):
                    w.writerow([i if i < 12 else i + 1, 10**18 + t * 10**6, '' if i == 2 else v, v])
        write('interval')
        stats = generate(folder, self.run)
        self.assertEqual((stats['common_gap_count'], stats['channel_gap_count']), (2, 5))
        info, = json.loads(Path(stats['outputs']['sampling_gaps']).read_text())['recordings']
        self.assertEqual([g['reason'] for g in info['channel_gaps'] if g['channel'] == 'A'],
                         ['通道缺值', 'RawTime 间隔', 'Index 缺号'])
        stats = generate(folder, self.run, pattern='A')
        self.assertEqual(stats['common_gap_count'], 2)
        write('event')
        stats = generate(folder, self.run)
        self.assertEqual(stats['common_gap_count'], 1)
        self.assertIn(('3', '9'), {(r['from值'], r['to值']) for r in self.rows(stats, 'jumps')})

    def test_filtered_sysmac_step_with_missing_values_keeps_other_recorded_channels(self):
        path = self.root / '20260101080000_00001.csv'
        for other, expected in [('TRUE', 0), ('', 1)]:
            with path.open('w', newline='', encoding='utf-8') as stream:
                w = csv.writer(stream)
                w.writerows([['TraceType', 'Continuous'], ['SampleTimingType', 'interval'],
                             ['Index', 'RawTime', 'A:INT', 'Switch:BOOL'],
                             [0, 1000000, 1, 'TRUE'], [1, 2000000, '', other], [2, 3000000, 2, 'TRUE']])
            info = generate(path, self.run, pattern='A')
            self.assertEqual((info['unique_pairs'], info['channel_gap_count'], info['common_gap_count']),
                             (0, 1, expected))

    def test_unresolved_or_sustained_slower_cadence_is_not_cut(self):
        self.assertEqual(clock_breaks([1, 4, 80, 200]), (None, set()))
        self.assertEqual(clock_breaks([1, 1, 11], specified=1), (1, {3}))
        self.assertFalse(clock_breaks([1] * 90 + [20] * 10)[1])
        self.assertFalse(clock_breaks([1] * 8 + [10] + [1] * 8)[1])
