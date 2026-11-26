import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.production_countercheck import production_counterchecks
from plc_trace.capture.staged_result import staged_result_checks
from tests.test_capture_activation import project, section, variable


class ProductionCountercheckTests(unittest.TestCase):
    host = 'CASE Main OF 1: IF Finished THEN Main:=3; END_IF; 3: Main:=1; END_CASE;'
    work = '''CASE Cursor OF
    14: Motor.Start:=TRUE; IF Motor.AtPosition THEN Cursor:=7; END_IF;
    7: IF NOT Jaw.Closed THEN Jaw.Close:=TRUE; END_IF; IF Jaw.Closed THEN Cursor:=6; END_IF;
    6: Finished:=TRUE; Cursor:=14;
    END_CASE;'''

    def check(self, host, work, *, closed=False, extra='', separate=False, peer_selected=True, checker=production_counterchecks):
        globals_ = [('Main', 'INT'), ('Cursor', 'INT'), ('Another', 'INT'), ('Sister', 'INT'), ('Service', 'BOOL'),
                    ('Request1', 'BOOL'), ('Request2', 'BOOL'), ('Finished', 'BOOL'), ('Motor', 'Drive'), ('Jaw', 'Grip'), ('Go', 'BOOL'), ('Bypass', 'BOOL')]
        def structure(typename, fields):
            return (f'<DataTypeDecl name="{typename}">' +
                    ''.join(f'<Member name="{f}"><Type><TypeName>BOOL</TypeName></Type></Member>' for f in fields) +
                    '</DataTypeDecl>')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'test.xml'
            source.write_text(project(structure('Drive', ['Start', 'AtPosition']), structure('Grip', ['Close', 'Closed']),
                                      '<GlobalVars>' + ''.join(variable(n, k) for n,k in globals_) + '</GlobalVars>',
                                      '<Program name="Line">' + section(host) +
                                      ('</Program><Program name="Requester">' if separate else '') +
                                      section(work + extra) + '</Program>'))
            generate(source, root / 'out')
            inventory = json.loads((root / 'out/evidence/step_variable_discovery.json').read_bytes())
        rows = {r['name']:r for r in inventory['candidates']}
        decisions = [{'name': 'Main', 'admission': 'captured', 'boundaries': [{'write': rows['Main']['transitions'][0]['id']}]},
                     {'name': 'Cursor', 'admission': 'optional'}]
        if not peer_selected:
            decisions[0]['admission'] = 'optional'
        if closed:
            decisions[1]['exclusion_evidence'] = {'kind': 'enclosed_child'}
        return checker(inventory, decisions).get('cursor')

    def test_successive_device_phases_publish_a_required_result_to_the_selected_operation(self):
        found = self.check(self.host, self.work)
        self.assertEqual((found['kind'], found['peer'], found['result']), ('consumed_completion', 'Main', 'finished'))
        self.assertEqual([s['feedback'] for s in found['stages']], ['motor.atposition', 'jaw.closed'])

    def test_feedback_presence_and_similar_step_values_alone_cannot_reopen_a_decision(self):
        cases = [
            (self.host.replace('IF Finished', 'IF Finished OR Bypass'), self.work, '', False),
            (self.host, self.work.replace('Cursor:=7;', 'Cursor:=99;'), '', False),
            (self.host, self.work.replace('Jaw.Closed THEN Cursor:=6', 'Bypass THEN Cursor:=6'), '', False),
            (self.host, self.work, 'CASE Another OF 8: Finished:=TRUE; Another:=9; 9: Another:=8; END_CASE;', False),
            (self.host, self.work, '', True),
            (self.host, 'IF Service THEN ' + self.work.replace('Finished:=TRUE;', '') +
             ' ELSE CASE Cursor OF 6: Finished:=TRUE; Cursor:=14; END_CASE; END_IF;', '', False),
        ]
        for host, work, extra, closed in cases:
            with self.subTest(host=host, work=work, extra=extra, closed=closed):
                self.assertIsNone(self.check(host, work, extra=extra, closed=closed))

    def test_parent_restart_and_completion_are_cited_only_for_multistage_children(self):
        host = ('CASE Main OF 1: Cursor:=14; Main:=2; 2: IF Cursor=6 THEN Main:=3; END_IF; '
                '3: Cursor:=14; Main:=2; END_CASE;')
        # Labels do not have to ascend. Both starts and the waiting write refer to this instance.
        found = self.check(host, self.work)
        self.assertEqual(found['kind'], 'restarted_child')
        self.assertEqual(len(found['starts']), 2)
        self.assertIsNone(self.check(host.replace('3: Cursor:=14;', '3:'), self.work))

    def test_parked_parent_leaves_two_independently_cycling_work_channels(self):
        host = 'CASE Main OF 1: IF Go THEN Main:=3; END_IF; 3: Cursor:=14; Sister:=8; Main:=20; END_CASE;'
        extra = 'CASE Sister OF 8: Motor.Start:=TRUE; Sister:=15; 15: Sister:=8; END_CASE;'
        found = self.check(host, self.work, extra=extra)
        self.assertEqual(found['kind'], 'recurring_jobs')
        self.assertIsNone(self.check(host, self.work, extra=extra.replace('Sister:=8;', 'Sister:=99;')))

    def test_request_and_result_bind_two_programs_to_the_actual_intervening_job(self):
        source = ('CASE Cursor OF 14: Request1:=TRUE; Request2:=TRUE; Cursor:=7; '
                  '7: IF Finished THEN Request1:=FALSE; Request2:=FALSE; Finished:=FALSE; Cursor:=14; END_IF; END_CASE;')
        worker = ('CASE Main OF 1: IF Request1 OR Request2 THEN Main:=8; END_IF; '
                  '8: Motor.Start:=TRUE; IF Motor.AtPosition THEN Main:=4; END_IF; '
                  '4: Jaw.Close:=TRUE; IF Jaw.Closed THEN Main:=5; END_IF; '
                  '5: Finished:=TRUE; Main:=1; END_CASE;')
        for selected in (True, False):
            proof = self.check(worker, source, separate=True, peer_selected=selected)
            self.assertEqual(proof['kind'], 'work_request_and_result')
            self.assertEqual(set(proof['requests']), {'request1', 'request2'})
            self.assertEqual(proof['peer'], 'Main')
        for peer, own in [
            (worker.replace('OR Request2', 'OR Bypass'), source),
            (worker.replace('Motor.Start:=TRUE;', '').replace('Jaw.Close:=TRUE;', ''), source),
            (worker.replace('Main:=5;', 'Main:=1;'), source),
            (worker, source.replace('Finished:=FALSE;', '')),
        ]:
            with self.subTest(peer=peer, own=own):
                self.assertIsNone(self.check(peer, own, separate=True))

    def test_staged_result_uses_two_separate_handoffs_and_an_enabled_device(self):
        host = ('CASE Main OF 1: Request1:=TRUE; Main:=3; '
                '3: IF Finished THEN Request1:=FALSE; Finished:=FALSE; Main:=8; END_IF; '
                '8: IF Request2 THEN Main:=1; END_IF; END_CASE;')
        worker = ('CASE Cursor OF 14: IF Request1 THEN Motor.AtPosition:=FALSE; Cursor:=23; END_IF; '
                  '23: IF Service THEN Motor.Start:=TRUE; END_IF; Cursor:=60; '
                  '60: Finished:=TRUE; Cursor:=99; '
                  '99: IF Motor.AtPosition OR NOT Service THEN '
                  'IF Service AND Motor.AtPosition THEN Request2:=TRUE; END_IF; Cursor:=14; END_IF; END_CASE;')
        proof = self.check(host, worker, separate=True, checker=staged_result_checks)
        self.assertEqual((proof['payload'], proof['sensor'], proof['enable']), ('request2', 'motor.atposition', 'service'))
        # A reviewer cited only the homing mode. Production still has its own device command
        # between consuming readiness and the workpiece result, at a separate step.
        separate_mode = ('IF Bypass THEN CASE Main OF 0: Main:=7; 7: Main:=0; END_CASE; ELSE ' +
                         host.replace('8: IF Request2', '8: Jaw.Close:=TRUE; IF Jaw.Closed THEN Main:=9; END_IF; '
                                      '9: IF Request2') + ' END_IF;')
        supported = self.check(separate_mode, worker, separate=True, checker=staged_result_checks)
        self.assertEqual(supported['peer_stage']['feedback'], 'jaw.closed')
        self.assertIsNone(self.check(separate_mode.replace('Jaw.Close:=TRUE;', ''), worker, separate=True,
                                     checker=staged_result_checks))
        for sender, responder in [
            (host.replace('IF Request2', 'IF Bypass'), worker),
            (host.replace('IF Request2', 'IF Bypass OR Request2'), worker),
            (host, worker.replace('Finished:=TRUE; Cursor:=99;', 'Finished:=TRUE; Cursor:=14;')),
            (host, worker.replace('Motor.Start:=TRUE;', 'Jaw.Close:=TRUE;')),
            (host, worker.replace('Motor.AtPosition:=FALSE;', '')),
            (host, worker.replace('Motor.AtPosition OR NOT Service', 'Motor.AtPosition OR Bypass')),
            (host, worker.replace('IF Service AND Motor.AtPosition', 'IF Service')),
        ]:
            with self.subTest(sender=sender, responder=responder):
                self.assertIsNone(self.check(sender, responder, separate=True, checker=staged_result_checks))
