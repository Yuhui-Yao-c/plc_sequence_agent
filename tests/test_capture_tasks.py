import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from plc_trace.capture.extract_step_variables import generate
from plc_trace.paths import RESOURCE_ROOT


class CaptureTaskTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / '工程.xml'
        self.out = self.root / 'out'

    def project(self, tasks, programs=None, extensions=''):
        if programs is None:
            programs = '<pou name="Sequence" pouType="program"><body><ST>Cell.Phase := 10;</ST></body></pou>'
        self.source.write_text(
            '<project xmlns="http://www.plcopen.org/xml/tc6_0200"><fileHeader productName="InoProShop"/>'
            f'<types><pous>{programs}</pous></types>{tasks}{extensions}</project>', encoding='utf-8')

    @staticmethod
    def resource(name, tasks):
        return f'<instances><configurations><configuration name="Device"><resource name="{name}">{tasks}' \
               '</resource></configuration></configurations></instances>'

    @staticmethod
    def extension(task):
        return '<addData><data name="http://www.3s-software.com/plcopenxml/task">' + task + '</data></addData>'

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

    def test_write_owner_and_explicit_sampler_have_independent_cadence(self):
        self.project(self.extension('''<task name="Writer" interval="PT0.008S" priority="2">
            <pouInstance name="Sequence" typeName=""/><addData><data><ObjectId>writer-id</ObjectId></data>
            </addData></task>''') + self.extension('''<task name="Sampler">
            <addData><data><pathStructure name="Device"><pathStructure name="App">
            <pathStructure name="Sampler"/></pathStructure></pathStructure></data>
            <data><TaskSettings KindOfTask="Cyclic" Interval="4000" IntervalUnit="µs"/></data>
            <data><ObjectId>sampler-id</ObjectId></data></addData></task>'''))
        automatic = self.run_setup()
        self.assertEqual((automatic['task_name'], automatic['nominal_sample_interval_ms'], automatic['buffer_entries']),
                         ('Writer', 8, 251))
        explicit = self.run_setup(task_name='sampler', every_n_cycles=3, force=True)
        self.assertEqual((explicit['task_name'], explicit['nominal_sample_interval_ms'], explicit['buffer_entries']),
                         ('Sampler', 12, 168))
        task = explicit['declared_tasks'][1]
        self.assertEqual((task['scope'], task['object_id']), (['Device', 'App'], 'sampler-id'))
        config = ET.parse(explicit['configuration'])
        self.assertNotEqual(config.findtext(".//Single[@Name='guidPOU']"), task['object_id'])
        self.assertEqual(config.findtext(".//Single[@Name='BufferEntries']"), '168')
        channel, = config.findall('./TraceData/TraceRecord/TraceVariable')
        self.assertEqual(channel.get('VarName'), 'Cell.Phase')
        self.assertFalse(channel.findtext('Values') or channel.findtext('Timestamps'))

    def test_undetermined_writers_do_not_select_a_template_name(self):
        self.project(self.resource('App', '<task name="MainTask"/><task name="Sampler B"/>'))
        report = self.run_setup()
        self.assertEqual((report['status'], report['configuration'], report['task_name']), ('needs_review', None, None))
        self.assertEqual((self.out / 'static_variables.txt').read_text().strip(), 'Cell.Phase')
        self.assertIn('Sampler B', report['task_note'])
        chosen = self.run_setup(task_name='sampler b', force=True)
        self.assertEqual((chosen['status'], chosen['task_name']), ('done', 'Sampler B'))

    def test_missing_declarations_preserve_channels_and_accept_a_known_task(self):
        self.project('<addData><data name="other"><task name="Noise"/></data></addData>')
        pending = self.run_setup()
        self.assertEqual((pending['status'], pending['configuration']), ('needs_review', None))
        self.assertFalse(list(self.out.glob('*.trace')))
        self.assertIn('未提供', pending['task_note'])
        report = self.run_setup(task_name='SiteClock', force=True)
        self.assertEqual((report['status'], report['task_name'], report['buffer_entries']), ('done', 'SiteClock', 501))
        self.assertFalse(report['task_verified_in_xml'])
        self.assertIsNone(report['nominal_sample_interval_ms'])

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
                self.assertEqual(report['buffer_entries'], 501 if expected is None else 10)
                self.assertEqual(report['buffer_entries_source'], '模板设置' if expected is None else '按名义采样间隔计算')

    def test_explicit_template_buffer_survives_known_cadence(self):
        self.project(self.resource('App', '<task name="Clock" interval="PT0.004S"/>'))
        template = ET.parse(RESOURCE_ROOT / 'templates' / 'inovance_trace.xml')
        template.find(".//Single[@Name='OverrideRTSBufferSize']").text = 'True'
        template.find(".//Single[@Name='BufferEntries']").text = '234'
        templates = self.root / 'templates'
        templates.mkdir()
        template.write(templates / 'inovance_trace.xml', encoding='utf-8')
        with patch('plc_trace.capture.extract_step_variables.RESOURCE_ROOT', self.root):
            report = self.run_setup(every_n_cycles=2)
        self.assertEqual((report['nominal_sample_interval_ms'], report['buffer_entries_source']), (8, '模板设置'))
        self.assertEqual(ET.parse(report['configuration']).findtext(".//Single[@Name='BufferEntries']"), '234')

    def test_detached_actions_follow_reachable_scoped_calls_and_ld_outputs(self):
        def action(program, name, body):
            return (f'<addData><data><action name="{name}"><body>{body}</body><addData>'
                    '<data name="http://www.3s-software.com/plcopenxml/pathstructure">'
                    f'<pathStructure name="App"><pathStructure name="{program}"><pathStructure name="{name}"/>'
                    '</pathStructure></pathStructure></data></addData></action></data></addData>')

        programs = ('<pou name="Fill"><body><ST>Dispatch();</ST></body></pou>'
                    '<pou name="Read"><body><ST>Dispatch();</ST></body></pou>')
        tasks = ('<task name="Sampling" interval="PT0.008S"><pouInstance name="live" typeName="Fill"/></task>'
                 '<task name="Other" interval="PT0.001S"><pouInstance name="Read"/></task>')
        extensions = (action('Fill', 'Dispatch', '<ST><xhtml>Output(); Dispatch();</xhtml></ST>') +
                      action('Read', 'Dispatch', '<ST>FB(Cell.Phase := Input); Seen := Cell.Phase;'
                             ' (* Output(); *) O . Output(); Log := \'Output()\';</ST>') +
                      action('Read', 'Output', '<ST>Cell.Phase := 0;</ST>') +
                      action('Fill', 'Output', '<LD><block><outputVariables><variable><connectionPointOut>'
                             '<expression>Cell.Phase</expression></connectionPointOut></variable></outputVariables>'
                             '</block></LD>'))
        self.project(self.resource('App', tasks), programs, extensions)
        report = self.run_setup()
        self.assertEqual(report['task_name'], 'Sampling')
        writer, reader = report['declared_tasks']
        self.assertEqual(writer['channel_writes']['Cell.Phase']['call_path'], ['Fill', 'Dispatch', 'Output'])
        self.assertGreater(writer['channel_writes']['Cell.Phase']['xml_line'], 0)
        self.assertEqual(reader['channel_writes'], {})

    def test_distinct_channel_counts_local_shadow_and_source_order(self):
        # Named input parameters, local variables, and repeated reads/writes must not win votes.
        programs = ('<pou name="One"><body><LD><block><addData><data><STCode>'
                    'Cell.Phase[1] := 2; F(IN := 1, Q =&gt; Cell.Phase[2]);'
                    '</STCode></data></addData></block></LD></body></pou>'
                    '<pou name="Two"><body><ST>Cell.Phase[3]:=1; Cell.Phase[3]:=2; t:=Cell.Phase[1];'
                    '</ST></body></pou><pou name="Local"><interface><localVars><variable name="Cell"/>'
                    '</localVars></interface><body><ST>Cell.Phase[9]:=0;</ST></body></pou>')
        entries = ['<task name="First" interval="PT0.016S"><pouInstance name="One"/></task>',
                   '<task name="MainTask" interval="PT0.004S"><pouInstance name="Two"/>'
                   '<pouInstance name="Local"/></task>']
        for order in (entries, entries[::-1]):
            self.project(self.resource('App', ''.join(order)), programs)
            status = generate(self.source, self.out, 'Cell.Phase[*]', force=True)
            self.assertEqual((status['task_name'], status['buffer_entries']), ('First', 126))
            self.assertEqual(status['channels_without_task_write'], ['Cell.Phase[9]'])
            self.assertEqual({t['name']: len(t['channel_writes']) for t in status['declared_tasks']},
                             {'First': 2, 'MainTask': 1})
            self.assertEqual(status['static_variables'], 4)
        tie = generate(self.source, self.out, ['Cell.Phase[1]', 'Cell.Phase[3]'], force=True)
        self.assertEqual((tie['status'], tie['configuration']), ('needs_review', None))
        self.assertIn('并列', tie['task_note'])

    def test_nested_actions_and_transitive_program_calls_cross_task_boundaries(self):
        programs = ('<pou name="Start"><body><LD><block typeName="Bridge"/></LD></body></pou>'
                    '<pou name="Bridge"><actions><action name="Cycle"><body><LD><outVariable>'
                    '<expression>Cell.Phase</expression></outVariable></LD></body></action></actions>'
                    '<body><ST>Cycle();</ST></body></pou>')
        first = '<task name="Scheduled"><pouInstance name="Start"/></task>'
        second = '<task name="Another"><pouInstance name="Bridge"/></task>'
        self.project(self.resource('App', first + second), programs)
        tie = self.run_setup()
        self.assertEqual(tie['status'], 'needs_review')
        self.assertEqual(tie['declared_tasks'][0]['channel_writes']['Cell.Phase']['call_path'],
                         ['Start', 'Bridge', 'Cycle'])
        self.project(self.resource('App1', first) + self.resource('App2', second), programs)
        self.assertIn('多个应用或资源', self.run_setup(force=True)['task_note'])

    def test_invalid_override_leaves_existing_configuration_intact(self):
        self.project(self.resource('App', '<task name="Clock"/>'))
        first = self.run_setup()
        config = Path(first['configuration'])
        before = config.read_bytes()
        for options in ({'task_name': 'Unknown'}, {'every_n_cycles': 0}, {'every_n_cycles': '1.5'},
                        {'every_n_cycles': 4294967296}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_setup(force=True, **options)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(json.loads((self.out / 'workflow_status.json').read_bytes()), first)


if __name__ == '__main__':
    unittest.main()
