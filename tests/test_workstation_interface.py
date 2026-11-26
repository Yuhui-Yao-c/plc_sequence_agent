import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.workstation_interface import WorkstationInterfaces
from tests.test_capture_coverage import neutral


class StationPreviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'cell.xml'
        self.out = self.root / 'setup'
        self.source.write_text('''<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10">
<GlobalVars>
<Variable name="工件阶段"><Type><TypeName>ARRAY[0..3] OF INT</TypeName></Type><Documentation>自动步序</Documentation></Variable>
<Variable name="Spare"><Type><TypeName>INT</TypeName></Type><Documentation>退出自动步序</Documentation></Variable>
</GlobalVars>
<Program name="Line"><Vars><Variable name="Cal"><Type><TypeName>INT</TypeName></Type><Documentation>自动步序</Documentation></Variable></Vars>
<BodyContent name="Transfer"><ST>
CASE 工件阶段[0] OF 1: IF Ready THEN 工件阶段[0]:=2; END_IF; 2: 工件阶段[0]:=1; END_CASE;
IF 工件阶段[1]=1 AND Load THEN 工件阶段[1]:=2; END_IF; Seen:=工件阶段[3];
CASE Spare OF 1: Spare:=2; 2: Spare:=1; END_CASE;
</ST></BodyContent>
<BodyContent name="SC_Manual"><ST>
CASE Cal OF 1: IF TestOn THEN Cal:=2; END_IF; 2: Cal:=1; END_CASE;
</ST></BodyContent></Program></Project>''', encoding='utf-8')

    def test_chinese_production_short_written_slot_and_full_review_inventory(self):
        result = generate(self.source, self.out)
        self.assertEqual(result['trace_targets_stage'], 'preview')
        self.assertEqual(Path(result['trace_targets']).read_text().splitlines(), ['工件阶段[0]', '工件阶段[1]'])
        ledger = json.loads((self.out / 'evidence/step_variable_discovery.json').read_bytes())
        self.assertTrue({'工件阶段[0]', 'Line.Cal', 'Spare'} <= {r['name'] for r in ledger['candidates']})
        # Even a review that presents a manual calibration as production must
        # connect its asserted boundary to work in normal production.
        scope = neutral(ledger)
        for row in scope['decisions']:
            if row['name'] in ('Line.Cal', '工件阶段[0]'):
                signal = 'TestOn' if row['name'] == 'Line.Cal' else 'Ready'
                row.update(role='component_sequence', use='production',
                           timing_basis=dict(scope='workstation', operation='交接', boundary_write='t1'),
                           boundaries=[dict(action='交接', write='t1', via='signal:' + signal)])
        path = self.root / 'scope.json'
        path.write_text(json.dumps(scope), encoding='utf-8')
        result = generate(self.source, self.out, scope_results=path)
        self.assertEqual(result['trace_targets_stage'], 'final')
        self.assertIn('工件阶段[0]', Path(result['trace_targets']).read_text().splitlines())
        self.assertNotIn('Line.Cal', Path(result['trace_targets']).read_text().splitlines())
        roles = json.loads((self.out / 'evidence/scope_results.json').read_bytes())['decisions']
        cal = next(r for r in roles if r['name'] == 'Line.Cal')
        self.assertEqual((cal['admission'], cal['exclusion_evidence']['kind']), ('review', 'service_section_review'))

    def test_independent_reference_can_include_a_nondefault_cursor(self):
        ref = self.root / 'specified.txt'
        ref.write_text('Line.Cal\n', encoding='utf-8')
        first = generate(self.source, self.out, reference=ref)
        self.assertIn('Line.Cal', Path(first['trace_targets']).read_text().splitlines())
        inv = json.loads((self.out / 'evidence/step_variable_discovery.json').read_bytes())
        path = self.root / 'review.json'
        path.write_text(json.dumps(neutral(inv)), encoding='utf-8')
        final = generate(self.source, self.out, reference=ref, scope_results=path)
        self.assertIn('Line.Cal', Path(final['trace_targets']).read_text().splitlines())
        self.assertEqual(final['coverage']['reference_missing_from_config'], [])


class EquipmentBoundaryTests(unittest.TestCase):
    def setUp(self):
        caller = 'P::Assembly'
        self.call = dict(scope=caller, instance='Drive', type='Feeder', source_line=15,
                         reference_actuals={'job': 'Tray', 'axis': 'Rack[3]'})
        self.station = dict(name='Tray.工步', identity='tray.工步', eligibility='scope_required',
                            sequence_witness={'kind': 'case_dispatch'}, effects=[],
                            declarations=[dict(kind='declaration', path='Tray.工步', text='automatic cycle step')],
                            transitions=[dict(scope=caller, program='P', guard='Ready')])
        base = dict(scope='P::Assembly::Drive', binding=[self.call])
        self.row = dict(name='P.Drive.Phase', identity='p.drive.phase', control_links=[], declarations=[],
                        sequence_witness=True, eligibility='scope_required',
                        transitions=[dict(base, program='P', guard='Drive.Phase=2 AND Rack[3].AtLoad', guard_complete=True)],
                        effects=[dict(base, target='Rack[3].Move', value='TRUE')])
        self.scan = SimpleNamespace(
            fb_fields={('feeder', 'phase'): ('localvars', {'line': 1}),
                       ('feeder', 'axis'): ('inoutvars', {'line': 7, 'text': 'servo motor'})},
            references={}, wired_operands={})

    def check(self):
        return WorkstationInterfaces([self.station, self.row], self.scan).equipment_cycle(self.row)

    def test_local_drive_feedback_is_not_an_independent_workstation(self):
        evidence = self.check()
        self.assertEqual(evidence['station_channels'], ['Tray.工步'])
        self.assertEqual(evidence['actuator_ports'], [{'line': 7, 'text': 'servo motor'}])

    def test_generic_step_comment_on_private_device_cannot_establish_a_station(self):
        self.row['declarations'] = [dict(kind='declaration', path=self.row['name'], text='自动步序')]
        interfaces = WorkstationInterfaces([self.station, self.row], self.scan)
        self.assertEqual(interfaces.channels, {'tray.工步'})
        self.assertIsNotNone(interfaces.equipment_cycle(self.row))

    def test_separately_dispatched_station_uses_its_concrete_write_site(self):
        write, = self.station['transitions']
        self.station.update(eligibility='distributed_write', transitions=[], sequence_witness=None,
                            dispatch_writes=[dict(value=2, write=write, dispatch=dict(scope='P::Observe'))])
        self.assertEqual(self.check()['station_channels'], ['Tray.工步'])

    def test_public_phase_and_external_consumption_preserve_normal_review(self):
        for group in ('outputvars', 'inoutvars'):
            with self.subTest(group=group):
                self.scan.fb_fields['feeder', 'phase'] = (group, {'line': 1})
                self.assertIsNone(self.check())
        self.scan.fb_fields['feeder', 'phase'] = ('localvars', {'line': 1})
        self.station['transitions'][0]['guard'] = 'Drive.HandedOff'
        self.assertIsNone(self.check())
        self.station['transitions'][0]['guard'] = 'Ready'
        self.scan.references[('DRIVE.PHASE', ('pou', 'P'))] = ()
        self.assertIsNone(self.check())

    def test_material_handoff_and_unrelated_station_cannot_be_closed_as_device_cycles(self):
        for changes in (dict(target='Tray.Received'), dict(target='Pallet[3].Taken'), dict(value='Lot.Identity')):
            item = self.row['effects'][0]
            self.row['effects'].append(dict(item, **changes))
            self.assertIsNone(self.check())
            self.row['effects'].pop()
        self.call['reference_actuals']['job'] = 'OtherTray'
        self.assertIsNone(self.check())
        self.call['reference_actuals']['job'] = 'Tray'
        self.scan.fb_fields['feeder', 'axis'] = ('inoutvars', {'line': 7, 'text': 'material handoff'})
        self.assertIsNone(self.check())

    def test_feedback_must_come_from_the_bound_device(self):
        self.row['transitions'][0]['guard_complete'] = False
        self.assertIsNone(self.check())
        self.row['transitions'][0]['guard_complete'] = True
        self.row['transitions'][0]['guard'] = 'Rack[2].AtLoad'
        self.assertIsNone(self.check())

    def test_additional_published_result_requires_review(self):
        self.row['other_outputs'] = ['station.transfercomplete']
        self.assertIsNone(self.check())
