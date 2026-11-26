import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.coordination_scope import coordination_only
from plc_trace.capture.extract_step_variables import generate
from tests.test_capture_activation import project, variable


def section(label, code, order):
    return f'<BodyContent name="{label}" evaluationOrder="{order}"><ST><![CDATA[{code}]]></ST></BodyContent>'


class CoordinationScopeTests(unittest.TestCase):
    def inventory(self, *bodies):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fields = ''.join(variable(k, 'INT') for k in ('Host', 'Alpha', 'Beta'))
            fields += ''.join(variable(k, 'BOOL') for k in ('Valve', 'Grip', 'Start', 'Vacuum'))
            fields += variable('Positions', 'ARRAY[0..12] OF BOOL')
            source = root / 'fixture.xml'
            source.write_text(project('<GlobalVars>' + fields + '</GlobalVars>', '<Program name="Packing">' +
                                      ''.join(section('Logic' + str(i), c, i) for i, c in enumerate(bodies)) + '</Program>'))
            generate(source, root / 'out')
            report = json.loads((root / 'out/evidence/step_variable_discovery.json').read_bytes())
            return {row['identity']: row for row in report['candidates']}

    def test_parent_must_start_independently_recurring_children_then_park(self):
        host = 'CASE Host OF 1: IF Start THEN Host:=40; END_IF; 40: Alpha:=3; Beta:=3; Host:=19; END_CASE;'
        jobs = ('CASE Alpha OF 3: Valve:=TRUE; Alpha:=8; 8: Alpha:=3; END_CASE; '
                'CASE Beta OF 3: Grip:=TRUE; Beta:=7; 7: Beta:=3; END_CASE;')
        for changed_host, changed_jobs, expected in (
                (host, jobs, True),
                (host + ' IF Start THEN Host:=1; END_IF;', jobs, False),
                (host.replace('Alpha:=3;', 'Vacuum:=TRUE; Alpha:=3;'), jobs, False),
                (host.replace('END_CASE;', '19: Host:=1; END_CASE;'), jobs, False),
                (host, jobs.replace('7: Beta:=3;', '7: Beta:=99;'), False)):
            with self.subTest(host=changed_host, jobs=changed_jobs):
                rows = self.inventory(changed_host + ' ' + changed_jobs)
                row = rows['host']
                writes = {w['from']: w['id'] for w in row['transitions']}
                link = next(l['id'] for l in row['control_links'] if l['kind'] == 'starts_child')
                claims = [{'write': writes[40], 'via': link}]
                proof = coordination_only(row, rows, claims)
                self.assertEqual(proof['kind'] if proof else None, 'launches_recurring_jobs' if expected else None)
                self.assertIsNone(coordination_only(row, rows, claims + [{'write': writes[1], 'via': 'signal:Start'}]))
                self.assertIsNone(coordination_only(row, rows, claims + [{'write': writes[40], 'via': 'comment'}]))

    def test_grouped_readiness_requires_closed_action_free_code_and_conjunctive_join(self):
        host = ('CASE Host OF 18: Alpha:=71; Beta:=5; Host:=29; '
                '29: IF Alpha=15 AND Beta=16 THEN Host:=18; END_IF; END_CASE;')
        child = ('CASE Alpha OF 71: IF Positions[1] AND Positions[3] THEN Alpha:=4; END_IF; '
                 '4: Alpha:=15; END_CASE;')
        other = 'CASE Beta OF 5: Grip:=TRUE; Beta:=16; END_CASE;'
        for parent, operation, expected in (
                (host, child, True),
                (host, child.replace('Positions[1] AND ', ''), False),
                (host, child.replace('Alpha:=4;', 'Valve:=TRUE; Alpha:=4;'), False),
                (host, child.replace('Alpha:=4;', 'Other(); Alpha:=4;'), False),
                (host, child.replace('Alpha:=4;', 'Message:=Alpha; Alpha:=4;'), False),
                (host + ' IF Alpha=15 THEN Vacuum:=TRUE; END_IF;', child, False),
                (host + ' Clock(IN:=Alpha=4);', child, False),
                (host + ' Vacuum:=Positions[Alpha];', child, False),
                (host + " Title:='Alpha=15'; (* IF Alpha=4 THEN *)", child, True),
                (host.replace('AND Beta=16', 'OR Beta=16'), child, False),
                (host.replace('AND Beta=16', ''), child, False)):
            with self.subTest(parent=parent, child=operation):
                rows = self.inventory(parent, operation, other)
                row = rows['alpha']
                proof = coordination_only(row, rows, [{'write': row['transitions'][0]['id'], 'via': 'comment'}])
                self.assertEqual(proof['kind'] if proof else None, 'joined_readiness' if expected else None)
