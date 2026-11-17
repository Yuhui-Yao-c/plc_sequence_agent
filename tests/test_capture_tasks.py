import json
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from plc_trace.capture.extract_step_variables import generate


class CaptureTaskTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / '工程.xml'
        self.reference = self.root / '参考 配置.trace'
        self.out = self.root / 'out'

    def project(self, tasks):
        self.source.write_text(
            '<project xmlns="http://www.plcopen.org/xml/tc6_0200"><fileHeader productName="InoProShop"/>'
            '<types><pous><pou name="Sequence" pouType="program"><body><ST>Cell.Phase := 10;</ST>'
            '</body></pou></pous></types>' + tasks + '</project>', encoding='utf-8')

    @staticmethod
    def resource(name, tasks):
        return f'<instances><configurations><configuration name="Device"><resource name="{name}">{tasks}' \
               '</resource></configuration></configurations></instances>'

    @staticmethod
    def extension(task):
        return '<addData><data name="http://www.3s-software.com/plcopenxml/task">' + task + '</data></addData>'

    def sample(self, task, cycles, *, encoding='utf-8'):
        self.reference.write_text(
            '<Trace><TraceConfiguration><Single><Single Name="Record">'
            f'<Single Name="TaskName">{task}</Single><Single Name="EveryNCycles">{cycles}</Single>'
            '<Single Name="guidPOU">00000000-0000-0000-0000-000000000123</Single>'
            '</Single></Single></TraceConfiguration><TraceData><TraceRecord>'
            '<TraceVariable VarName="Other.Chan"><Values>2,3</Values><Timestamps>1,2</Timestamps>'
            '</TraceVariable></TraceRecord></TraceData></Trace>', encoding=encoding)

    def run_setup(self, **options):
        return generate(self.source, self.out, 'Cell.Phase', **options)

    def test_single_declared_task_program_bindings_and_submillisecond_schedule(self):
        self.project(self.resource('App', '<task name="Fast Loop" interval="PT0.0005S" priority="7">'
                                   '<pouInstance name="Line" typeName="Sequence"/></task>'))
        report = self.run_setup(every_n_cycles=4)
        self.assertEqual((report['task_name'], report['task_name_source']), ('Fast Loop', '源码唯一任务'))
        task, = report['declared_tasks']
        self.assertEqual(task['scope'], ['Device', 'App'])
        self.assertEqual(task['pou_instances'], [{'name': 'Line', 'type_name': 'Sequence'}])
        self.assertEqual((task['period_ms'], report['nominal_sample_interval_ms']), (0.5, 2))
        self.assertTrue(report['task_verified_in_xml'])
        record = ET.parse(report['configuration']).find(".//Single[@Name='Record']")
        self.assertEqual(record.findtext("./Single[@Name='TaskName']"), 'Fast Loop')
        self.assertEqual(record.findtext("./Single[@Name='EveryNCycles']"), '4')

    def test_reference_selects_sampler_independently_of_program_and_channels(self):
        self.project(self.extension('''<task name="Writer" interval="PT0.008S" priority="2">
            <pouInstance name="Sequence" typeName=""/><addData><data><ObjectId>writer-id</ObjectId></data>
            </addData></task>''') + self.extension('''<task name="Sampler">
            <addData><data><pathStructure name="Device"><pathStructure name="App">
            <pathStructure name="Sampler"/></pathStructure></pathStructure></data>
            <data><TaskSettings KindOfTask="Cyclic" Interval="4000" IntervalUnit="µs"/></data>
            <data><ObjectId>sampler-id</ObjectId></data></addData></task>'''))
        self.sample('sampler', 3, encoding='utf-16')
        report = self.run_setup(task_from_trace=self.reference)
        self.assertEqual((report['task_name'], report['task_name_source'], report['nominal_sample_interval_ms']),
                         ('Sampler', '参考 Trace', 12))
        task = report['declared_tasks'][1]
        self.assertEqual((task['scope'], task['object_id']), (['Device', 'App'], 'sampler-id'))
        config = ET.parse(report['configuration'])
        self.assertEqual(config.findtext(".//Single[@Name='EveryNCycles']"), '3')
        self.assertNotEqual(config.findtext(".//Single[@Name='guidPOU']"), task['object_id'])
        self.assertNotEqual(config.findtext(".//Single[@Name='guidPOU']"), '00000000-0000-0000-0000-000000000123')
        channel, = config.findall('./TraceData/TraceRecord/TraceVariable')
        self.assertEqual(channel.get('VarName'), 'Cell.Phase')
        self.assertFalse(channel.findtext('Values') or channel.findtext('Timestamps'))
        overridden = self.run_setup(task_from_trace=self.reference, every_n_cycles=2, force=True)
        self.assertEqual((overridden['every_n_cycles_source'], overridden['nominal_sample_interval_ms']), ('用户指定', 8))

    def test_default_must_be_declared_when_task_list_is_available(self):
        self.project(self.resource('App', '<task name="Slow"/><task name="MAINTASK" interval="PT0.001S"/>'))
        matched = self.run_setup()
        self.assertEqual((matched['task_name'], matched['nominal_sample_interval_ms']), ('MAINTASK', 1))
        self.assertTrue(matched['task_verified_in_xml'])
        self.project(self.resource('App', '<task name="Sampler A"/><task name="Sampler B"/>'))
        report = self.run_setup(force=True)
        self.assertEqual(report['status'], 'needs_review')
        self.assertIsNone(report['configuration'])
        self.assertIsNone(report['task_name'])
        self.assertEqual((self.out / 'static_variables.txt').read_text().strip(), 'Cell.Phase')
        self.assertIn('Sampler B', report['task_note'])
        chosen = self.run_setup(task_name='sampler b', force=True)
        self.assertEqual((chosen['status'], chosen['task_name']), ('done', 'Sampler B'))

    def test_missing_declarations_keep_legacy_option_visibly_unverified(self):
        self.project('<addData><data name="other"><task name="Noise"/></data></addData>')
        for options, expected in (({}, 'MainTask'), ({'task_name': 'SiteClock'}, 'SiteClock')):
            report = self.run_setup(**options, force=True)
            self.assertEqual((report['status'], report['task_name']), ('done', expected))
            self.assertFalse(report['task_verified_in_xml'])
            self.assertIsNone(report['nominal_sample_interval_ms'])
            self.assertIn('未提供', report['task_note'])

    def test_duplicate_objects_and_ambiguous_resources(self):
        task = ('<task name="Clock" interval="PT1S"><addData><data><ObjectId>one</ObjectId>'
                '</data></addData></task>')
        self.project(self.resource('App A', task) + self.extension(task))
        self.assertTrue(self.run_setup()['task_verified_in_xml'])
        self.project(self.resource('App A', task) + self.resource('App B', task.replace('>one<', '>two<')))
        report = self.run_setup(force=True)
        self.assertEqual((report['status'], report['configuration']), ('needs_review', None))
        self.assertIn('同名', report['task_note'])
        with self.assertRaisesRegex(ValueError, '同名'):
            self.run_setup(task_name='Clock', force=True)

    def test_event_or_conflicting_periods_have_no_nominal_milliseconds(self):
        for attributes, settings, expected in (
                ('single="Rising"', '', None),
                ('interval="PT0.02S"', 'KindOfTask="Event" Interval="t#20ms"', None),
                ('interval="PT0.02S"', 'KindOfTask="Cyclic" Interval="t#10ms"', None),
                ('', 'KindOfTask="Cyclic" Interval="T#1s_20ms"', 1020),
                ('', 'KindOfTask="Cyclic" Interval="4"', None)):
            with self.subTest(attributes=attributes, settings=settings):
                self.project(self.extension(f'<task name="Clock" {attributes}><addData><data>'
                                            f'<TaskSettings {settings}/></data></addData></task>'))
                report = self.run_setup(force=True)
                self.assertEqual(report['nominal_sample_interval_ms'], expected)

    def test_invalid_reference_or_override_leaves_existing_configuration_intact(self):
        self.project(self.resource('App', '<task name="Clock"/>'))
        first = self.run_setup()
        config = Path(first['configuration'])
        before = config.read_bytes()
        for task, cycles in (('Unknown', 1), ('Clock', 0), ('Clock', '1.5'), ('Clock', 4294967296)):
            self.sample(task, cycles)
            with self.subTest(task=task, cycles=cycles), self.assertRaises(ValueError):
                self.run_setup(task_from_trace=self.reference, force=True)
        self.reference.write_text('<Trace><TraceConfiguration><Single><Single Name="Record"/>'
                                  '<Single Name="Record"/></Single></TraceConfiguration></Trace>', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Record'):
            self.run_setup(task_from_trace=self.reference, force=True)
        with self.assertRaisesRegex(ValueError, '只能指定其一'):
            self.run_setup(task_from_trace=self.reference, task_name='Clock', force=True)
        with self.assertRaisesRegex(ValueError, '输出目录之外'):
            self.run_setup(task_from_trace=config, force=True)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(json.loads((self.out / 'workflow_status.json').read_bytes()), first)


if __name__ == '__main__':
    unittest.main()
