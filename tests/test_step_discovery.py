import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.step_discovery import Discovery, replay_selectors, scope_selection
from plc_trace.capture.station_scope import dependent_writes
from plc_trace.cli import _expand_trace_shorthand
from tests.test_plcopen_trace_control import ladder, operand, port, program


class StepDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'Line.xml'
        self.out = self.root / 'out'

    def prepare(self, xml):
        self.source.write_text(xml, encoding='utf-8')
        result = generate(self.source, self.out)
        return result, json.loads((self.out / 'step_variable_discovery.json').read_text(encoding='utf-8'))

    @staticmethod
    def scope(inventory, names):
        decisions, examples = [], {}
        for row in inventory['candidates']:
            if row['eligibility'] != 'scope_required':
                continue
            item = {'name': row['name'], 'role': 'uncertain', 'basis': ['t1'], 'reason': '缺少本单元周期依据'}
            if row['name'] in names:
                scope = row['transitions'][0]['scope']
                evidence = [*row['declarations'], *(t for t in row['transitions'] if t['scope'] == scope),
                            *(e for e in row['effects'] if e['scope'] == scope)]
                item.update(role='station_auto', basis=[v['id'] for v in evidence], owner_scope=scope,
                            reason='此实例推进已核对的作业周期')
                examples.setdefault(row['family'], {'example': row['name'], 'boundary': '此单元从接料到交出工件'})
            decisions.append(item)
        return {'inventory_sha256': inventory['inventory_sha256'],
                'station_families': list(examples.values()), 'decisions': decisions}

    def finish(self, inventory, names):
        path = self.root / 'scope.json'
        path.write_text(json.dumps(self.scope(inventory, names)))
        return generate(self.source, self.out, scope_results=path)

    def test_default_route_and_explicit_selector_are_distinct(self):
        parser = argparse.ArgumentParser()
        self.assertEqual(_expand_trace_shorthand(parser, ['cell.xml', '--trace-setup'])[:2],
                         ['trace-setup', '--source=cell.xml'])
        self.assertEqual(_expand_trace_shorthand(parser, ['cell.xml', 'a', 'b', '--trace-setup'])[2:4],
                         ['--pattern=a', '--pattern=b'])
        self.assertEqual(_expand_trace_shorthand(parser, ['cell.xml', 'record.trace', '-t'])[0], 'trace')

    def test_scope_proof_local_identity_monitor_and_counter(self):
        ctrl = '<interface><localVars><variable name="K"><type><INT/></type></variable></localVars></interface>'
        body = ('<body><ST>CASE K OF 2: IF Ready THEN K:=7; Valve:=TRUE; END_IF; '
                '7: IF ReturnSignal THEN K:=2; END_IF; END_CASE; '
                'IF BestAutoStep=60 THEN BestAutoStep:=0; END_IF; '
                'IF K=99 THEN Recorded:=TRUE; END_IF; (* Fake := 20; *)</ST></body>')
        xml = ('<project><fileHeader productName="InoProShop"/><globalVars><variable name="BestAutoStep">'
               '<type><INT/></type></variable></globalVars>' + program('Left', ctrl+body) + program('Right', ctrl+body)
               + '<resource><task name="Cycle"><pouInstance name="Left"/><pouInstance name="Right"/></task></resource></project>')
        status, inv = self.prepare(xml)
        rows = {r['name']: r for r in inv['candidates']}
        self.assertEqual(status['status'], 'scope_review_pending')
        self.assertIsNone(status['configuration'])
        self.assertEqual({k for k, r in rows.items() if r['eligibility'] == 'scope_required'}, {'Left.K', 'Right.K'})
        self.assertTrue(all(e['target'] == 'Valve' for e in rows['Left.K']['effects']))
        self.assertEqual(self.finish(inv, {'Left.K'})['channels'][0]['name'], 'Left.K')
        self.assertTrue(list(self.out.glob('*.trace')))
        self.assertEqual(json.loads((self.out/'scope_results.json').read_bytes())['inventory_sha256'],
                         inv['inventory_sha256'])

    def test_sysmac_typed_rebinding_across_two_calls_and_readonly_formal(self):
        def var(name, typ):
            return f'<Variable name="{name}"><Type><TypeName>{typ}</TypeName></Type></Variable>'
        def st(code):
            return '<BodyContent name="cycle"><ST>' + code + '</ST></BodyContent>'
        ladder_body = 'CASE v OF 4: IF ok THEN flag:=TRUE; v:=9; END_IF; 9: v:=4; END_CASE;'
        xml = ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+var('Z','INT')+'</GlobalVars>'
               '<FunctionBlock name="Worker"><InOutVars>'+var('v','INT')+'</InOutVars>'+st(ladder_body)+'</FunctionBlock>'
               '<FunctionBlock name="Wrapper"><InOutVars>'+var('p','INT')+'</InOutVars><LocalVars>'+var('obj','Worker')
               +'</LocalVars>'+st('obj(v:=p);')+'</FunctionBlock><FunctionBlock name="Copy"><InputVars>'
               +var('v','INT')+'</InputVars>'+st(ladder_body)+'</FunctionBlock><Program name="Site"><LocalVars>'
               +var('outer','Wrapper')+var('ro','Copy')+var('decoy','INT')+'</LocalVars>'+st('outer(p:=Z); ro(v:=decoy);')
               +'</Program></Project>')
        _, inv = self.prepare(xml)
        rows = [r for r in inv['candidates'] if r['eligibility'] == 'scope_required']
        self.assertEqual([r['name'] for r in rows], ['Z'])
        self.assertEqual([c['instance'] for c in rows[0]['transitions'][0]['binding']], ['outer', 'obj'])
        self.assertEqual(self.finish(inv, {'Z'})['channels'][0]['type'], 'INT')

    def test_wired_fb_can_nominate_a_name_absent_from_all_comparisons(self):
        ref = '<interface><inOutVars><variable name="V"><type><INT/></type></variable></inOutVars></interface>'
        fb = '<pou name="Unit" pouType="functionBlock">'+ref+'<body><ST>CASE V OF 1: V:=4; 4: V:=1; END_CASE;</ST></body></pou>'
        caller = ladder(operand(11,'Cold')+'<block localId="12" typeName="Unit" instanceName="Slot"><inputVariables>'
                        +port('EN',0)+port('V',11)+'</inputVariables></block>')
        status, inv = self.prepare('<project><fileHeader productName="InoProShop"/><globalVars><variable name="Cold">'
                                   '<type><INT/></type></variable></globalVars>'+fb+program('Site',caller)+
                                   '<resource><task name="Task"><pouInstance name="Site"/></task></resource></project>')
        row, = [r for r in inv['candidates'] if r['eligibility'] == 'scope_required']
        self.assertEqual((row['name'], row['edges']), ('Cold', 2))
        self.assertEqual(row['transitions'][0]['binding'][0]['reference_actuals'], {'v':'Cold'})
        self.assertEqual(self.finish(inv, {'Cold'})['task_name'], 'Task')

    def test_cross_run_and_extra_names_rejected_and_selector_cannot_expand(self):
        d = Discovery()
        c = d.candidate('A')
        c.edge(8, 9, {'scope': 'run'})
        c.edge(9, 8, {'scope': 'run'})
        c.declarations = [{'kind': 'declaration', 'path': 'A', 'text': '工程声明', 'line': 18}]
        inv = d.inventory('source', 'unit')
        scope = self.scope(inv, {'A'})
        for field, value in [('name', 'B'), ('role', 'mixed'), ('basis', ['t1']),
                             ('basis', ['t1', 'd2']), ('basis', ['d1']), ('owner_scope', 'other')]:
            bad = copy.deepcopy(scope)
            bad['decisions'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                scope_selection(inv, bad)
        for field, value in [('inventory_sha256', 'old'), ('decisions', []), ('station_families', []),
                             ('station_families', [{'example': 'B', 'boundary': 'other'}])]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                scope_selection(inv, {**scope, field: value})
        rules = replay_selectors(['Z[1]', 'Z[3]'], ['Z[1]', 'Z[2]', 'Z[3]'])
        self.assertIsNone(rules[0]['selector'])
        self.assertEqual(rules[0]['other_matches'], ['z[2]'])

    def test_missing_decl_and_unreachable_syntax_never_produce_configuration(self):
        xml = ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><Program name="P"><BodyContent>'
               '<ST>CASE U OF 2: U:=3; 3: U:=2; END_CASE; RETURN; '
               'CASE Fake OF 1: Fake:=2; 2: Fake:=1; END_CASE;</ST></BodyContent></Program></Project>')
        _, inv = self.prepare(xml)
        self.assertNotIn('fake', {r['identity'] for r in inv['candidates'] if r['eligibility']=='scope_required'})
        status = self.finish(inv, {'U'})
        self.assertEqual(status['status'], 'needs_review')
        self.assertIn('U', status['unresolved_types'])
        self.assertIsNone(status['configuration'])

    def test_unguarded_alternative_and_uncalled_writer_cannot_promote(self):
        var = '<variable name="U"><type><INT/></type></variable>'
        decl = '<globalVars>'+var+var.replace('"U"', '"Z"')+'</globalVars>'
        unreachable = program('Spare', '<body><ST>CASE U OF 1: U:=2; 2: U:=1; END_CASE;</ST></body>')
        sampled = program('Live', '<body><ST>IF (Z=1 OR Bypass) THEN Z:=3; END_IF;</ST></body>')
        _, inv = self.prepare('<project><fileHeader productName="InoProShop"/>'+decl+unreachable+sampled+
                              '<resource><task name="T"><pouInstance name="Live"/></task></resource></project>')
        self.assertFalse(any(r['eligibility']=='scope_required' for r in inv['candidates']))
        row = next(r for r in inv['candidates'] if r['name']=='U')
        self.assertIn('no_reachable_control_writer_in_exported_schedule', row['reasons'])

    def test_counted_loop_retains_enclosing_gate_but_cannot_prove_its_own_target(self):
        text = ('CASE K OF 11: IF Ready THEN FOR I:=1 TO 3 DO A[I]:=0; END_FOR; K:=12; END_IF; '
                '12: K:=11; END_CASE; FOR I:=1 TO 3 DO CASE Z OF 2: Z:=3; 3: Z:=2; END_CASE; END_FOR;')
        declarations = ''.join('<Variable name="'+n+'"><Type><TypeName>INT</TypeName></Type></Variable>' for n in ['K','Z'])
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+declarations+
                              '</GlobalVars><Program name="Line"><BodyContent><ST>'+text+'</ST></BodyContent></Program></Project>')
        rows = {r['name']:r for r in inv['candidates'] if r['eligibility']=='scope_required'}
        self.assertEqual(set(rows), {'K'})
        edge = next(t for t in rows['K']['transitions'] if t['from']==11)
        self.assertIn('Ready', edge['guard'])
        self.assertFalse(edge['guard_complete'])
        self.assertEqual(inv['parse_limits']['unsupported']['counted_loop_contents_omitted'],1)

    def test_shared_links_have_unique_ids_within_each_candidate(self):
        d = Discovery()
        for n in ('P','A','B'):
            row = d.candidate(n)
            row.edge(1,2,{'scope':'station','source_line':1})
            row.edge(2,1,{'scope':'station','source_line':2})
        for child in ('B','A'):
            d.possible_links.append(('P',1,child,2,'waits_for_child',{'scope':'station','source_line':3}))
        inv = d.inventory('sha','unit')
        for row in inv['candidates']:
            refs=[v['id'] for v in row['control_links']]
            self.assertEqual(len(refs),len(set(refs)))
        p=next(r for r in inv['candidates'] if r['name']=='P')
        self.assertEqual([x['id'] for x in p['control_links']],['l1','l2'])

    def test_declared_only_project_publishes_empty_review(self):
        status, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
                                   '<Variable name="Step"><Type><TypeName>INT</TypeName></Type></Variable>'
                                   '</GlobalVars></Project>')
        self.assertEqual(status['status'],'needs_review')
        self.assertEqual(inv['candidates'],[])
        self.assertIsNone(status['configuration'])

    def test_mixed_plcopen_st_keeps_state_before_opaque_loop_and_does_not_skip_return(self):
        body = ('<body><ST>IF Logged THEN Clear(x:=Buf); END_IF; '
                'CASE Cursor OF 2: IF Armed THEN Pulse:=TRUE; Cursor:=4; END_IF; '
                '4: IF Ack THEN Cursor:=6; END_IF; 6: FOR I:=1 TO 8 DO Clear(x:=Buf); END_FOR; '
                'Cursor:=2; END_CASE; RETURN; CASE Never OF 2: Never:=8; 8: Never:=2; END_CASE;</ST></body>')
        gl = '<globalVars>'+''.join('<variable name="'+n+'"><type><INT/></type></variable>' for n in ('Cursor','Never'))+'</globalVars>'
        _, inv = self.prepare('<project><fileHeader productName="InoProShop"/>'+gl+program('Site',body)+
                              '<resource><task name="T"><pouInstance name="Site"/></task></resource></project>')
        rows = {r['name']:r for r in inv['candidates'] if r['eligibility']=='scope_required'}
        self.assertEqual(set(rows), {'Cursor'})
        self.assertEqual({(e['from'],e['to']) for e in rows['Cursor']['transitions']},{(2,4),(4,6)})
        self.assertTrue(all(e['kind']=='plcopen_partial_st_body' for e in rows['Cursor']['transitions']))
        self.assertTrue(any(e['target']=='Pulse' for e in rows['Cursor']['effects']))
        self.assertGreater(inv['parse_limits']['unsupported_st'],0)

    def test_station_scope_blocks_nested_word_and_keeps_parallel_cycles(self):
        control = ('CASE AxisX OF 2: IF Ready THEN AxisX:=9; END_IF; '
                   '9: IF ExitOK THEN AxisX:=2; END_IF; END_CASE; '
                   'IF AxisX=2 THEN CASE Ticket OF 1: Ticket:=5; 5: Ticket:=1; END_CASE; END_IF; '
                   'CASE Lane OF 4: IF AxisX=2 OR BufferFree THEN Lane:=6; END_IF; '
                   '6: Lane:=4; END_CASE;')
        decl = ''.join(f'<Variable name="{n}"><Type><TypeName>INT</TypeName></Type></Variable>'
                       for n in ('AxisX', 'Lane', 'Ticket'))
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+decl+
                              '</GlobalVars><Program name="Bay"><BodyContent name="Cycle"><ST>'+control+
                              '</ST></BodyContent></Program></Project>')
        result = self.finish(inv, {'AxisX', 'Lane', 'Ticket'})
        self.assertEqual({c['name'] for c in result['channels']}, {'AxisX','Lane'})
        self.assertEqual(result['scope_uncertain'], 1)
        decision = json.loads((self.out/'scope_results.json').read_bytes())['decisions']
        self.assertEqual(next(r for r in decision if r['name']=='Ticket')['gated_by'], ['axisx'])
        self.assertEqual(result['status'], 'done_with_review')
        import csv
        with Path(result['configuration']).open(encoding='utf-8-sig', newline='') as f:
            header = next(r for r in csv.reader(f) if r and r[0] == 'Index')
        self.assertEqual(header[5:], ['AxisX:INT','Lane:INT'])

    def test_per_element_notes_and_individually_scoped_instances(self):
        var = ('<Variable name="Cursor"><Documentation>运行序列</Documentation><AddData><Data><VariableComment>'
               '<ElementComment element="[0]"><Text>工位作业周期</Text></ElementComment>'
               '<ElementComment element="[1]"><Text>工装内部交接</Text></ElementComment>'
               '</VariableComment></Data></AddData><Type><TypeName>ARRAY[0..3] OF INT</TypeName></Type></Variable>')
        code = ' '.join(f'CASE Cursor[{i}] OF 3: Cursor[{i}]:=8; 8: Cursor[{i}]:=3; END_CASE;' for i in (0,1))
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+var+
                              '</GlobalVars><Program name="U"><BodyContent name="Cycle"><ST>'+code+
                              '</ST></BodyContent></Program></Project>')
        rows = {r['name']:r for r in inv['candidates']}
        self.assertEqual([s['text'] for s in rows['Cursor[0]']['declarations']], ['运行序列','工位作业周期'])
        self.assertEqual([s['text'] for s in rows['Cursor[1]']['declarations']], ['运行序列','工装内部交接'])
        scope = self.scope(inv, {'Cursor[0]'})
        item = next(r for r in scope['decisions'] if r['name']=='Cursor[1]')
        item.update(role='component_sequence', basis=['d2','t1'])
        chosen, reviewed = scope_selection(inv, scope)
        self.assertEqual(set(chosen), {'cursor[0]'})
        self.assertEqual(reviewed[1]['admission'], 'excluded')
        scope['decisions'][0]['basis'].remove('d2')
        with self.assertRaises(ValueError):
            scope_selection(inv, scope)

    def test_equal_member_names_keep_own_instance_declarations(self):
        def struct(name, note):
            return f'<variable name="{name}"><type><derived name="U"/></type><documentation>{note}</documentation></variable>'
        types = ('<dataType name="U"><baseType><struct><variable name="Mark"><type><INT/></type>'
                 '<documentation>位置</documentation></variable></struct></baseType></dataType>')
        code = ' '.join(f'CASE {name}.Mark OF 7: {name}.Mark:=11; 11: {name}.Mark:=7; END_CASE;'
                        for name in ('Head','Drive'))
        _, inv = self.prepare('<project><fileHeader productName="InoProShop"/>'+types+'<globalVars>'+
                              struct('Head', '装配单元')+struct('Drive', '移送元件')+'</globalVars>'+
                              program('Line', '<body><ST>'+code+'</ST></body>')+
                              '<resource><task name="T"><pouInstance name="Line"/></task></resource></project>')
        rows = {r['name']:r for r in inv['candidates']}
        self.assertEqual([d['text'] for d in rows['Head.Mark']['declarations']], ['装配单元', '位置'])
        self.assertEqual([d['text'] for d in rows['Drive.Mark']['declarations']], ['移送元件', '位置'])
        self.assertEqual(self.finish(inv, {'Head.Mark'})['static_variables'], 1)

    def test_registered_slot_in_own_unit_with_foreign_array_and_program(self):
        var = '<Variable name="{}"><Type><TypeName>ARRAY[0..5] OF INT</TypeName></Type></Variable>'
        def cycle(member):
            return f'CASE {member} OF 2: {member}:=7; 7: {member}:=2; END_CASE;'
        def prog(name, code):
            return f'<Program name="{name}"><BodyContent name="Work"><ST>{code}</ST></BodyContent></Program>'
        code = cycle('Q[0]') + ' IF Q[0]=2 THEN '+cycle('Q[1]')+cycle('Part[1]')+' END_IF;'
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+
                              var.format('Q')+var.format('Part')+'</GlobalVars>'+prog('P',code)+
                              prog('Other',cycle('Q[3]'))+'</Project>')
        names = {'Q[0]','Q[1]','Q[3]','Part[1]'}
        scope = self.scope(inv, names)
        lookup = {r['name']:r for r in scope['decisions']}
        lookup['Q[1]'].update(role='station_slot', interface_peer='Q[0]')
        selected, decisions = scope_selection(inv, scope)
        self.assertIn('q[1]', selected)
        self.assertNotIn('part[1]', selected)
        slot = next(r for r in decisions if r['name']=='Q[1]')
        self.assertEqual((slot['admission'],slot['gated_by']), ('captured',['q[0]']))
        for peer in ('Q[1]', 'Part[1]', 'Q[3]', 'absent'):
            with self.subTest(peer=peer), self.assertRaises(ValueError):
                lookup['Q[1]']['interface_peer'] = peer
                scope_selection(inv, scope)

    def test_shared_fb_body_keeps_call_instances_distinct_for_dominance(self):
        def site(instance, line, *peers):
            return {'scope': 'Reusable::Cycle', 'co_cursors': list(peers),
                    'binding': [{'scope': 'Line::Run', 'instance': instance, 'source_line': line}]}
        model = Discovery()
        word = model.candidate('Word')
        parent = model.candidate('Cursor')
        parent.edge(4, 9, site('First', 30, 'word'))
        parent.edge(4, 9, site('Second', 41))
        word.edge(1, 2, site('First', 30, 'cursor'))
        self.assertEqual(len(parent.sites), 2)
        rows = {r.name.casefold(): r.record() for r in (parent,word)}
        self.assertEqual(dependent_writes(rows), {})
        parent.edge(9, 4, site('First', 30))
        rows['cursor'] = parent.record()
        self.assertEqual(dependent_writes(rows), {'word':['cursor']})
