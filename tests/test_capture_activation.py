import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.extract_step_variables import generate
from tests.test_capture_coverage import neutral


def variable(name, kind, initial=''):
    return f'<Variable name="{name}"><Type><TypeName>{kind}</TypeName></Type>{initial}</Variable>'


def section(code):
    return f'<BodyContent><ST><![CDATA[\n{code}\n]]></ST></BodyContent>'


def project(*units):
    return '<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10">\n' + '\n'.join(units) + '\n</Project>'


class ActivationTests(unittest.TestCase):
    def run_project(self, xml, review=None, reference=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / 'cell.xml'
        source.write_text(xml)
        out = root / 'out'
        options = {}
        if reference:
            required = root / 'reference.txt'
            required.write_text(reference + '\n')
            options['reference'] = required
        preview = generate(source, out, **options)
        inventory = json.loads((out / 'step_variable_discovery.json').read_bytes())
        decisions = neutral(inventory)
        if review:
            review(decisions, xml)
        form = root / 'review.json'
        form.write_text(json.dumps(decisions))
        final = generate(source, out, scope_results=form, **options)
        roles = {r['name']: r for r in json.loads((out / 'scope_results.json').read_bytes())['decisions']}
        return preview, final, roles

    def test_only_closed_private_storage_can_be_pruned_at_its_actual_initial_value(self):
        core = """IF Cancel THEN Indexer:=0; END_IF;
(* Indexer:=12; (* nested *) Indexer:=9; *)
Message:='Indexer:=9;';
CASE Indexer OF 0: ; 7: IF Ready THEN Action:=TRUE; Indexer:=9; END_IF;
9: Indexer:=7; END_CASE;"""
        local = '<Vars accessSpecifier="private">'
        scenarios = [
            ('closed', local, '', '', False),
            ('independent start', local, '', section('IF Demand THEN Indexer:=7; END_IF;'), True),
            ('external write', local, '', '</Program><Program name="Other">'+section('Cell.Indexer:=7;'), True),
            ('block output', local, '', section('Driver(Result=>Indexer);'), True),
            ('inout', local, '', section('Driver(Buffer:=Indexer);'), True),
            ('address', local, '', section('Pointer:=ADR(Indexer);'), True),
            ('bit write', local, '', section('Indexer.0:=TRUE;'), True),
            ('wired write', local, '', '<BodyContent><LD><VendorOperand>Indexer</VendorOperand></LD></BodyContent>', True),
            ('initial seven', local, '<InitialValue><SimpleValue value="7"/></InitialValue>', '', True),
            ('retained', '<Vars accessSpecifier="private" retain="true">', '', '', True),
            ('shared', '<Vars>', '', '', True),
        ]
        def review(decisions, xml):
            for row in decisions['decisions']:
                row.update(role='component_sequence', use='production',
                           boundaries=[{'write': 't1', 'action': '等待夹紧到位', 'via': 'signal:Ready'}],
                           timing_basis={'scope': 'workstation', 'operation': '夹紧工步', 'boundary_write': 't1'})
        for label, group, initial, extra, keep in scenarios:
            with self.subTest(label=label):
                xml = project('<Program name="Cell">' + group + variable('Indexer', 'INT', initial) +
                              '</Vars>' + section(core) + extra + '</Program>')
                preview, final, roles = self.run_project(xml, review)
                self.assertEqual(preview['preview_configured_variables'], 1)
                self.assertEqual(final['configured_variables'], int(keep))
                proof = roles['Cell.Indexer'].get('exclusion_evidence', {})
                self.assertEqual(proof.get('kind'), None if keep else 'inactive_from_initial')
        xml = project('<Program name="Cell">'+local+variable('Indexer', 'INT')+'</Vars>'+section(core)+'</Program>')
        _, final, roles = self.run_project(xml, reference='Cell.Indexer')
        self.assertEqual(roles['Cell.Indexer']['admission'], 'retained')
        self.assertEqual(final['coverage']['reference_missing_from_config'], [])

    def test_interlocks_and_service_evidence_belong_to_each_instance(self):
        run = """// 维修模式下按键启动单循环
Delay(IN:=Selected AND Button, PT:=T#1S);
Pulse(CLK:=Delay.Q);
IF Pulse.Q THEN Cursor:=5; Running:=TRUE; END_IF;
IF NOT Selected OR Fault THEN Running:=FALSE; END_IF;
CASE Cursor OF 0: ;
5: IF Running THEN JawClose:=TRUE; END_IF; IF Ready THEN Cursor:=6; END_IF;
6: Cursor:=0; END_CASE;"""
        def review(decisions, xml):
            def cite(fragment):
                line = next(i for i, value in enumerate(xml.splitlines(), 1) if fragment in value)
                return {'line':line, 'text':fragment}
            for d in decisions['decisions']:
                proof = {'kind':'mode', 'gate':'Selected', 'purpose':cite('维修模式下按键启动单循环'),
                         'operation':cite('JawClose:=TRUE;')}
                if d['name'] == 'Cell.Right.Cursor':
                    proof['purpose'] = {**proof['purpose'], 'text':'并不存在的维护声明'}
                d.update(role='component_sequence', use='service', service_evidence=proof)
        fb = ('<FunctionBlock name="Worker"><Vars accessSpecifier="private">' +
              ''.join(variable(n,t) for n,t in [('Cursor','INT'),('Running','BOOL'),('Delay','TON'),('Pulse','R_TRIG')]) +
              '</Vars>')
        caller = '<Program name="Cell"><Vars>'+variable('Left','Worker')+variable('Right','Worker')+'</Vars>'
        cases = [
            ('mode required', run, '', 'optional'),
            ('automatic bypass', run.replace('Selected AND Button','(Selected AND Button) OR Work'), '', 'review'),
            ('other launch', run+'\nIF Work THEN Cursor:=6; END_IF;', '', 'review'),
            ('stop needs both', run.replace('NOT Selected OR Fault','NOT Selected AND Fault'), '', 'review'),
            ('overridden timer output', run.replace('Pulse(CLK', 'Delay.Q:=Work;\nPulse(CLK'), '', 'review'),
            ('timer not evaluated every scan', run.replace('Delay(IN:=','IF Enabled THEN Delay(IN:=')
             .replace('PT:=T#1S);','PT:=T#1S); END_IF;'), '', 'review'),
            ('unknown clock consumer', run.replace('Pulse(CLK', 'Other(IN:=Delay.Q);\nPulse(CLK'), '', 'review'),
            ('separate latch writer', run+'\nIF Work THEN Running:=TRUE; END_IF;', '', 'review'),
            ('latch may be written by reference', run+'\nOther(Output=>Running);', '', 'review'),
            ('escaped instance', run, 'PassByRef(Item:=Left);', 'review'),
            ('external latch write', run, 'Left.Running:=Work;', 'review'),
            ('external timer write', run, 'Left.Delay.Q:=Work;', 'review'),
            ('missing enabling latch', run.replace('IF Running THEN JawClose','IF Ready THEN JawClose'), '', 'review'),
        ]
        for label, code, extra, expected in cases:
            with self.subTest(label=label):
                xml = project(fb+section(code)+'</FunctionBlock>', caller+section('Left(); Right(); '+extra)+'</Program>')
                _, final, roles = self.run_project(xml, review)
                self.assertEqual(roles['Cell.Left.Cursor']['admission'], expected)
                self.assertEqual(roles['Cell.Right.Cursor']['admission'], 'review')
                self.assertEqual(final['static_variables'], 0)
        retained = caller.replace('<Vars>', '<RetainVars>').replace('</Vars>', '</RetainVars>')
        _, _, roles = self.run_project(project(fb+section(run)+'</FunctionBlock>',
                                              retained+section('Left(); Right();')+'</Program>'), review)
        self.assertEqual(roles['Cell.Left.Cursor']['admission'], 'review')

    def test_equipment_review_binds_authored_call_comment_and_executed_instruction(self):
        body = """CASE Position OF 0: IF Ready THEN Position:=3; END_IF;
3: SendFrame:=TRUE; Position:=0; END_CASE;"""
        block = ('<FunctionBlock name="Worker"><Vars accessSpecifier="private">'+variable('Position','INT')+
                 '</Vars>'+section(body)+'</FunctionBlock>')
        def review(decisions, xml):
            lines = xml.splitlines()
            def cite(text):
                return {'line':next(i for i, s in enumerate(lines, 1) if text in s), 'text':text}
            for d in decisions['decisions']:
                descriptor = '周期读取诊断帧' if d['name']=='Cell.Left.Position' else '更新保养计数'
                d.update(role='auxiliary_sequence', use='service',
                         service_evidence={'kind':'equipment','purpose':cite(descriptor),
                                           'operation':cite('SendFrame:=TRUE;')})
        host = '<Program name="Cell"><Vars>'+variable('Left','Worker')+variable('Right','Worker')+'</Vars>'
        left = "// 周期读取诊断帧\nLeft();"
        right = "Message:='更新保养计数';\nRight();"
        for label, calls, admit in [
                ('distinct callers', section(left)+section(right), 'optional'),
                ('ambiguous call site', section(left+'\n'+right), 'review'),
                ('production request reaches instance', section(left.replace('Left();','IF Work THEN Left(); END_IF;'))+
                 section(right), 'review')]:
            with self.subTest(label=label):
                # Give separate sections their actual source identities, as in a controller export.
                sections = calls.replace('<BodyContent>', '<BodyContent name="Diagnostics">', 1)
                sections = sections.replace('<BodyContent>', '<BodyContent name="Work">', 1)
                xml = project(block, host+sections+'</Program>')
                _, _, roles = self.run_project(xml, review)
                self.assertEqual(roles['Cell.Left.Position']['admission'], admit)
                self.assertEqual(roles['Cell.Right.Position']['admission'], 'review')
        # The same diagnostic instruction also occurs unconditionally in this POU.
        # A citation to that copy cannot justify removing the guarded production cursor.
        mixed = block.replace('CASE Position OF', 'SendFrame:=TRUE;\nCASE Position OF')
        scoped = host+section(left).replace('<BodyContent>', '<BodyContent name="Diagnostics">')+section(right)+'</Program>'
        _, _, roles = self.run_project(project(mixed, scoped), review)
        self.assertEqual(roles['Cell.Left.Position']['admission'], 'review')

    def test_parent_context_requires_the_exact_child_write_and_retains_unknown_parent_sources(self):
        from plc_trace.capture.coverage import _parent_context

        launch = {'line':40,'section_line':25,'offset':52,'guard':'Parent=9','value':4}
        link = {'kind':'starts_child','child':'Cell.Child','child_value':4,'guard':'Parent=9',
                'source_line':25,'offset':52,'parent':'Parent','parent_step':9}
        row = {'identity':'cell.child','control_links':[link], 'activation':{'entries':[launch],'open_lines':[]}}
        parent = {'name':'Parent','transitions':[]}
        reason, modes = _parent_context(row, {'parent':parent}, None)
        self.assertIn('Parent=9', reason)
        self.assertEqual(modes, [])
        parent['transitions'] = [{'id':'t1','to':9,'guard':'Parent=5 AND Calibrate'},
                                 {'id':'t2','to':9,'guard':'Parent=7 AND Calibrate AND Ready'}]
        class EngineeringDeclarations:
            def declarations(self, name):
                return [{'path':'Calibrate','text':'现场标定启用','line':10}] if name=='calibrate' else []
        reason, modes = _parent_context(row, {'parent':parent}, EngineeringDeclarations())
        self.assertIsNone(reason)
        self.assertEqual([(m['guard'], m['incoming']) for m in modes], [('calibrate', ['t1','t2'])])
        self.assertEqual(_parent_context({**row, 'control_links':[{**link, 'offset':53}]},
                                         {'parent':parent}, EngineeringDeclarations()), (None, []))

    def test_parent_service_gate_must_cover_every_confirmed_route(self):
        from plc_trace.capture.coverage import _parent_context, _service_mode

        self.assertTrue(_service_mode('整机标定模式 0是屏蔽 1是启用'))
        self.assertFalse(_service_mode('标定模式 0启用 1禁用'))
        self.assertFalse(_service_mode('not in maintenance mode'))

        def writer(offset, step, guard):
            link = {'kind': 'starts_child', 'child': 'Cell.Mark', 'parent': 'Root', 'parent_step': step,
                    'child_value': 7, 'guard': guard, 'source_line': 10, 'offset': offset}
            event = {'section_line': 10, 'offset': offset, 'guard': guard, 'value': 7}
            return link, event

        links, starts = zip(writer(3, 15, 'Root=15'), writer(7, 32, 'Root=32'))
        row = {'identity': 'cell.mark', 'control_links': links, 'activation': {'entries': starts, 'open_lines': []}}
        parent = {'name': 'Root', 'transitions': [
            {'id': 't1', 'to': 15, 'guard': 'Root=3 AND Switch', 'guard_complete': True},
            {'id': 't2', 'to': 32, 'guard': 'Root=6 AND Switch', 'guard_complete': True}]}

        class Declarations:
            def declarations(self, symbol):
                return [{'path': 'Switch', 'text': '标定模式', 'line': 4}] if symbol == 'switch' else []

        check = lambda: _parent_context(row, {'root': parent}, Declarations())
        self.assertIn('标定', check()[0])
        parent['transitions'][1].update(guard='Root=6 AND (Switch OR Demand)')
        self.assertIsNone(check()[0])
        parent['transitions'].pop(0)
        self.assertIsNone(check()[0])

    def test_explicit_idle_slot_is_recovered_only_in_the_witnessed_array_and_declared_bounds(self):
        body = 'CASE Used[1] OF 0: Used[1]:=4; 4: Used[1]:=0; END_CASE;'
        declared = lambda n: ('<variable name="'+n+'"><type><array><dimension lower="-1" upper="3"/>'
                             '<baseType><INT/></baseType></array></type></variable>')
        plc = ('<project><fileHeader productName="InoProShop"/><globalVars>'+declared('Used')+declared('Other')+
               '</globalVars><pou name="Line" pouType="program"><body><ST>'+body+'</ST></body></pou>'
               '<resource><task name="Cycle"><pouInstance name="Line"/></task></resource></project>')
        native = project('<GlobalVars>'+variable('Used','ARRAY[-1..3] OF INT')+variable('Other','ARRAY[-1..3] OF INT')+
                         '</GlobalVars>', '<Program name="Line">'+section(body)+'</Program>')
        for xml in (plc, native):
            with self.subTest(xml=xml[:40]):
                preview, final, _ = self.run_project(xml, reference='Used[-1]\nUsed[4]\nOther[-1]')
                self.assertEqual(preview['trace_target_count'], 2)
                self.assertEqual(final['coverage']['reference_missing_from_list'], ['Other[-1]', 'Used[4]'])
                recovered, = final['coverage']['reference_recovered']
                self.assertEqual((recovered['name'], recovered['type']), ('Used[-1]', 'INT'))
                self.assertIn('[-1, 3]', recovered['evidence'])
                self.assertEqual(final['configured_variables'], 1)
