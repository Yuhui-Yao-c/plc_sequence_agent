import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.capture_use import _guard_signals
from plc_trace.capture.command_interface import CommandInterfaces
from plc_trace.capture.extract_step_variables import generate
from tests.test_capture_coverage import neutral


def member(name, datatype):
    return f'<Member name="{name}"><Type><TypeName>{datatype}</TypeName></Type></Member>'


def declaration(name, datatype):
    return f'<Variable name="{name}"><Type><TypeName>{datatype}</TypeName></Type></Variable>'


def fixture():
    fields = member('Dispatch', 'Commands') + member('分料', 'INT') + member('Drive', 'Actuator')
    cmds = member('Recipe', 'INT') + member('阶段', 'INT')
    axis = member('Move', 'BOOL') + member('Ready', 'BOOL')
    types = ''.join(f'<DataTypeDecl name="{name}"><UserDefinedTypeSpec>{members}</UserDefinedTypeSpec></DataTypeDecl>'
                    for name, members in [('Machine', fields), ('Commands', cmds), ('Actuator', axis)])
    program = '''
CASE Cell.Dispatch.Recipe OF
5: CASE Cell.Dispatch.阶段 OF
     0: Cell.Drive.Move:=TRUE; Cell.Dispatch.阶段:=1;
     1: IF Cell.Drive.Ready THEN Cell.Dispatch.阶段:=0; END_IF;
   END_CASE;
8: CASE Cell.Dispatch.阶段 OF
     0: Cell.分料:=2; Helper:=10; Cell.Dispatch.阶段:=1;
     1: IF Helper=90 THEN Cell.Dispatch.阶段:=2; END_IF;
     2: IF Cell.分料=4 THEN Cell.Dispatch.阶段:=0; END_IF;
   END_CASE;
END_CASE;
IF Cell.Dispatch.Recipe=8 THEN
 CASE Cell.分料 OF 2: Cell.Drive.Move:=TRUE; Cell.分料:=3; 3: IF Cell.Drive.Ready THEN Cell.分料:=4; END_IF; END_CASE;
 CASE Helper OF 10: IF Ready THEN Helper:=20; END_IF; 20: Helper:=90; END_CASE;
END_IF;
'''
    return ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10">' + types + '<GlobalVars>' +
            declaration('Cell', 'Machine') + declaration('Ready', 'BOOL') + '</GlobalVars>'
            '<Program name="装配"><Vars accessSpecifier="private">' + declaration('Helper', 'INT') +
            '</Vars><BodyContent name="Operate"><ST>' + program + '</ST></BodyContent></Program></Project>')


class CommandInterfaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'cell.xml'
        self.source.write_text(fixture(), encoding='utf-8')

    def prepare(self, out):
        status = generate(self.source, out)
        return json.loads((out / status['discovery']).read_bytes())

    def test_local_completion_and_independent_chinese_station_members(self):
        out = self.root / 'automatic'
        ledger = self.prepare(out)
        self.assertEqual(set((out / 'static_variables.txt').read_text().splitlines()), {'Cell.Dispatch.阶段', 'Cell.分料'})
        scope = neutral(ledger)
        rows = {r['name']: r for r in ledger['candidates']}
        for d in scope['decisions']:
            r = rows[d['name']]
            signal = 'Ready' if d['name'] == '装配.Helper' else 'Cell.Drive.Ready'
            w = next(w for w in r['transitions'] if signal.casefold() in _guard_signals(w['guard']))
            child = next((l['id'] for l in r['control_links'] if l['child'] == r['name']), None)
            timing = {'scope': 'workstation', 'operation': '工件分料', 'boundary_write': w['id']}
            if child:
                timing.update(scope='production_substep', parent_link=child, parent_relation='上层读取完成值')
            d.update(role='component_sequence', basis=[w['id']], reason='按反馈记录完成', use='production',
                     boundaries=[dict(write=w['id'], action='完成分料', via='signal:'+signal)], timing_basis=timing)
        review = self.root / 'scope.json'
        review.write_text(json.dumps(scope))
        result = generate(self.source, out, scope_results=review)
        self.assertEqual(result['trace_targets_stage'], 'final')
        self.assertEqual({c['name'] for c in result['channels']}, {'Cell.Dispatch.阶段', 'Cell.分料'})
        roles = json.loads((out / 'evidence/scope_results.json').read_bytes())['decisions']
        helper = next(d for d in roles if d['name'] == '装配.Helper')
        self.assertEqual((helper['effective_use'], helper['exclusion_evidence']['kind']), ('unresolved', 'station_terminal_review'))
        reference = self.root / 'commissioned.txt'
        reference.write_text('装配.Helper\n', encoding='utf-8')
        explicit = generate(self.source, self.root / 'requested', reference=reference, scope_results=review)
        self.assertIn('装配.Helper', {c['name'] for c in explicit['channels']})
        selected = json.loads((self.root / 'requested/evidence/scope_results.json').read_bytes())['decisions']
        self.assertEqual(next(d['effective_use'] for d in selected if d['name'] == '装配.Helper'), 'reference')

    def test_selector_and_station_ownership_require_bound_executable_evidence(self):
        ledger = self.prepare(self.root / 'setup')
        rows = ledger['candidates']
        main = next(r for r in rows if r['name'] == 'Cell.Dispatch.阶段')
        child = next(r for r in rows if r['name'] == 'Cell.分料')
        self.assertEqual(CommandInterfaces(rows).owners, {'cell.dispatch.阶段': 'cell', 'cell.分料': 'cell'})
        for change in ('unguarded alternative', 'different station', 'only echoes command record'):
            copy = json.loads(json.dumps(main))
            if change == 'only echoes command record':
                for e in copy['effects']:
                    e['global_target'] = 'cell.dispatch.ack'
            else:
                for w in copy['transitions']:
                    if change == 'unguarded alternative':
                        w['operating_guard'] = '(' + w['operating_guard'] + ') OR Bypass'
                    else:
                        w['global_reads'] = ['other.dispatch.recipe']
            with self.subTest(change=change):
                self.assertFalse(CommandInterfaces([copy, child]).owners)
        child['control_links'] = []
        self.assertEqual(set(CommandInterfaces(rows).owners), {'cell.dispatch.阶段'})

    def test_open_and_intermediate_private_progress_can_be_reviewed(self):
        rows = self.prepare(self.root / 'setup')['candidates']
        helper = next(r for r in rows if r['name'] == '装配.Helper')
        commands = CommandInterfaces(rows)
        self.assertEqual(commands.private_protocol(helper)['terminal'], 90)
        helper['activation']['open_lines'] = [42]
        self.assertIsNone(commands.private_protocol(helper))
        helper['activation']['open_lines'] = []
        helper['activation']['read_uses'].append({'kind': 'parent_exit', 'value': 20})
        self.assertIsNone(commands.private_protocol(helper))


    def test_private_workpiece_stages_have_an_independent_completion_boundary(self):
        two_tools = fixture().replace(member('Drive', 'Actuator'), member('Drive', 'Actuator') + member('Tool', 'Actuator'))
        worker = ('10: Cell.Drive.Move:=TRUE; IF Cell.Drive.Ready THEN Helper:=20; END_IF; '
                  '20: Cell.Tool.Move:=TRUE; IF Cell.Tool.Ready THEN Helper:=30; END_IF; '
                  '30: Finished:=TRUE; Helper:=90;')
        two_tools = two_tools.replace('10: IF Ready THEN Helper:=20; END_IF; 20: Helper:=90;', worker)
        two_tools = two_tools.replace(declaration('Ready', 'BOOL'), declaration('Ready', 'BOOL') + declaration('Finished', 'BOOL'))
        for kind, logic, gate in [
            ('consumed_completion', two_tools.replace('2: IF Cell.分料=4', '2: IF Finished'), 'Finished'),
            ('parallel_job', two_tools.replace('1: IF Helper=90 THEN Cell.Dispatch.阶段:=2; END_IF;',
              '1: IF Cell.分料=4 THEN IF Helper=90 THEN Cell.Dispatch.阶段:=0; END_IF; END_IF;'), 'Helper'),
        ]:
            with self.subTest(kind=kind):
                self.source.write_text(logic, encoding='utf-8')
                out = self.root / kind
                ledger = self.prepare(out)
                rows = {r['name']: r for r in ledger['candidates']}
                helper = rows['装配.Helper']
                self.assertIsNotNone(CommandInterfaces(rows.values()).private_protocol(helper))
                scope = neutral(ledger)
                for decision in scope['decisions']:
                    name = decision['name']
                    if name not in ('Cell.Dispatch.阶段', '装配.Helper'):
                        continue
                    row = rows[name]
                    sig = gate if row is not helper else 'Cell.Tool.Ready'
                    write = next(w['id'] for w in row['transitions'] if sig.casefold() in _guard_signals(w['guard']))
                    timing = dict(scope='workstation', operation='分料', boundary_write=write)
                    if row is helper:
                        parent = next(l for l in row['control_links'] if l['kind'] == 'waits_for_child')
                        timing.update(scope='production_substep', parent_link=parent['id'], parent_relation='分别记录独立动作完成')
                    decision.update(use='production', role='component_sequence', basis=[write], reason='工件完成分料',
                                    boundaries=[dict(write=write, via='signal:'+sig, action='分料完成')], timing_basis=timing)
                submitted = self.root / (kind + '.json')
                submitted.write_text(json.dumps(scope))
                generate(self.source, out, scope_results=submitted)
                normalized = json.loads((out / 'evidence/scope_results.json').read_bytes())
                result = next(d for d in normalized['decisions'] if d['name'] == '装配.Helper')
                self.assertEqual(result['admission'], 'captured')
                self.assertEqual(result['timing_corroboration']['kind'], kind)

    def test_private_terminal_review_applies_to_ascii_identifiers_too(self):
        self.source.write_text(fixture().replace('装配', 'Assembly'), encoding='utf-8')
        rows = self.prepare(self.root / 'english')['candidates']
        worker = next(r for r in rows if r['name'] == 'Assembly.Helper')
        self.assertEqual(CommandInterfaces(rows).private_protocol(worker)['terminal'], 90)
