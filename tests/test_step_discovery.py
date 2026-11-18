import argparse
import json
import tempfile
import unittest
from pathlib import Path

from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.step_discovery import Discovery, replay_selectors, scope_selection
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

    def finish(self, inventory, names):
        path = self.root / 'scope.json'
        decisions = [{'name': row['name'], 'role': 'production' if row['name'] in names else 'uncertain',
                      'basis': ['t1'], 'reason': '源码分支推进并驱动输出'}
                     for row in inventory['candidates'] if row['eligibility'] == 'scope_required']
        path.write_text(json.dumps({'inventory_sha256': inventory['inventory_sha256'], 'decisions': decisions}))
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
        inv = {'inventory_sha256':'key', 'candidates':[{'identity':'a','name':'A','eligibility':'scope_required',
               'transitions':[{'id':'t1'}], 'effects':[], 'control_links':[]}]}
        for key, decisions in [('old', []), ('key', []), ('key', [{'name':'B'}]),
                               ('key',[{'name':'A','role':'production','basis':['invented'],'reason':'x'}])]:
            with self.assertRaises(ValueError):
                scope_selection(inv, {'inventory_sha256':key,'decisions':decisions})
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
