import argparse
import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from plc_trace.capture.extract_step_variables import generate
from plc_trace.capture.step_discovery import Discovery, replay_selectors, scope_selection
from plc_trace.capture.station_scope import dependent_writes
from plc_trace.cli import _expand_trace_shorthand
from tests.test_plcopen_trace_control import ladder, operand, port, program


def timing_basis(action, write='t1', row=None):
    basis = {'scope': 'workstation', 'operation': action, 'boundary_write': write}
    if row:
        parent = next((link for link in row['control_links'] if link['child'].casefold() == row['identity']), None)
        if parent:
            basis.update(scope='production_substep', parent_link=parent['id'],
                         parent_relation='工装交接有独立到位条件，上层工位继续时才能结束该工装的交接阶段')
    return basis


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
                t = row['transitions'][0]
                notes = [d for d in row['declarations'] if d['path'].casefold() == row['identity'] and d.get('text')]
                via = next(('signal:' + signal for signal in ('Ready', 'ok', 'BufferFree')
                            if signal.casefold() in t.get('guard', '').casefold()), None)
                if via or notes:
                    item.update(use='production', timing_basis=timing_basis('工装交接', t['id'], row),
                                boundaries=[{'action':'工装交接', 'write':t['id'],
                                                                'via':via or notes[-1]['id']}])
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

    def test_scope_retry_preserves_the_run_and_rolls_back_failed_publication(self):
        xml = ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
               '<Variable name="Cursor"><Type><TypeName>INT</TypeName></Type></Variable></GlobalVars>'
               '<Program name="Bay"><BodyContent name="Cycle"><ST>CASE Cursor OF '
               '1: IF Ready THEN Cursor:=4; END_IF; 4: IF Done THEN Cursor:=1; END_IF; END_CASE;'
               '</ST></BodyContent></Program></Project>')
        initial, inventory = self.prepare(xml)
        self.assertEqual(initial['status'], 'scope_review_pending')
        preview = Path(initial['preview_configuration'])
        self.assertEqual(initial['preview_configured_variables'], 1)
        self.assertEqual(Path(initial['trace_targets']).read_text().splitlines(), ['Cursor'])
        self.assertIn('Cursor:INT', preview.read_text(encoding='utf-8-sig'))
        scope = self.out / 'scope_results.json'
        scope.write_text(json.dumps(self.scope(inventory, {'Cursor'})))
        note = self.out / '现场笔记.txt'
        note.write_text('现场补充', encoding='utf-8')
        original = {f.name: f.read_bytes() for f in self.out.iterdir()}
        self.assertEqual(generate(self.source, self.out), initial)
        self.assertEqual(scope.read_bytes(), original[scope.name])

        replace = Path.replace
        def simulate_locked_status(source, destination):
            if source.parent.name == 'ready' and destination.name == 'workflow_status.json':
                raise PermissionError('locked status')
            return replace(source, destination)
        with patch.object(Path, 'replace', simulate_locked_status), self.assertRaisesRegex(PermissionError, 'locked status'):
            generate(self.source, self.out, scope_results=scope)
        self.assertEqual({f.name: f.read_bytes() for f in self.out.iterdir()}, original)
        self.assertEqual(set(self.root.iterdir()), {self.source, self.out})

        final = generate(self.source, self.out, scope_results=scope)
        self.assertFalse(preview.exists())
        self.assertFalse(Path(initial['trace_targets']).exists())
        self.assertEqual(Path(final['trace_targets']).read_text().splitlines(), ['Cursor'])
        self.assertEqual(final['static_variables'], 1)
        self.assertEqual(generate(self.source, self.out, scope_results=scope), final)
        self.assertEqual(generate(self.source, self.out), final)
        self.assertEqual(note.read_bytes(), original[note.name])
        self.assertEqual(set(self.root.iterdir()), {self.source, self.out})
        Path(final['configuration']).write_bytes(b'edited')
        with self.assertRaisesRegex(ValueError, '缺失或已修改'):
            generate(self.source, self.out, choose_available=True)
        self.assertEqual(set(self.root.iterdir()), {self.source, self.out})

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

    def test_legacy_pending_status_requires_the_same_recomputed_evidence(self):
        decl = '<interface><localVars><variable name="K"><type><INT/></type></variable></localVars></interface>'
        body = ('<body><ST>CASE K OF 2: IF Ready THEN K:=7; Valve:=TRUE; END_IF; '
                '7: IF Done THEN K:=2; END_IF; END_CASE;</ST></body>')
        status, inventory = self.prepare('<project><fileHeader productName="InoProShop"/>'
                                         + program('Line', decl + body)
                                         + '<resource><task name="Clock"><pouInstance name="Line"/></task></resource></project>')
        marker = self.out / 'workflow_status.json'
        legacy = {k:v for k,v in status.items() if k not in {'request_sha256', 'artifact_sha256', 'trace_targets',
                  'trace_target_count', 'preview_configuration', 'preview_configured_variables', 'preview_unresolved_types'}}
        legacy['version'] = '14.0.0'
        for later_file in ('optional_variables.csv', 'capture_context.csv', Path(status['trace_targets']).name,
                           Path(status['preview_configuration']).name):
            (self.out / later_file).unlink()  # Absent in the earlier pending fixture.
        marker.write_text(json.dumps(dict(legacy, inventory_sha256='different')))
        with self.assertRaisesRegex(ValueError, '候选证据'):
            self.finish(inventory, {'Line.K'})
        self.assertFalse(list(self.out.glob('*.trace')))
        marker.write_text(json.dumps(legacy))
        final = self.finish(inventory, {'Line.K'})
        self.assertEqual([r['name'] for r in final['channels']], ['Line.K'])
        self.assertTrue(final['artifact_sha256'])
        self.assertFalse(list(self.root.glob('*.previous-*')))

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

    def test_fb_child_start_and_wait_follow_the_same_bound_instance(self):
        def var(name, typ):
            return f'<Variable name="{name}"><Type><TypeName>{typ}</TypeName></Type></Variable>'

        code = ('CASE Owner OF 1: Cursor:=10; Owner:=2; '
                '2: IF Cursor=30 THEN Owner:=1; END_IF; END_CASE; '
                'CASE Cursor OF 10: IF Ready THEN Cursor:=30; END_IF; 30: Cursor:=0; END_CASE;')
        xml = ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
               +var('A','INT')+var('B','INT')+'</GlobalVars><FunctionBlock name="Unit"><InOutVars>'
               +var('Owner','INT')+'</InOutVars><Vars>'+var('Cursor','INT')+'</Vars><BodyContent><ST>'
               +code+'</ST></BodyContent></FunctionBlock><Program name="Site"><Vars>'
               +var('Left','Unit')+var('Right','Unit')+'</Vars><BodyContent><ST>'
               'Left(Owner:=A); Right(Owner:=B);</ST></BodyContent></Program></Project>')
        _, inventory = self.prepare(xml)
        for owner, child in [('A','Site.Left.Cursor'), ('B','Site.Right.Cursor')]:
            row = next(r for r in inventory['candidates'] if r['name'] == child)
            self.assertEqual({(l['kind'], l['parent'], l['parent_step'], l['child'], l['child_value'])
                              for l in row['control_links']},
                             {('starts_child', owner, 1, child, 10), ('waits_for_child', owner, 2, child, 30)})

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
               '<ST>CASE U OF 2: IF Ready THEN U:=3; END_IF; 3: U:=2; END_CASE; RETURN; '
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

    def test_loop_local_exit_keeps_independent_later_steps_and_pou_return_stops_them(self):
        from plc_trace.capture.step_discovery import isolate_counted_loops
        loop = 'FOR I:=4 TO 1 BY -1 DO IF Sensors[I] THEN Chosen:=I; EXIT; END_IF; END_FOR;'
        tail = ' CASE Bay OF 0: IF Part THEN Bay:=10; END_IF; 10: Bay:=0; END_CASE;'
        for instruction in ('EXIT', 'CONTINUE'):
            body = loop.replace('EXIT', instruction) + tail
            masked, writes = isolate_counted_loops(body)
            self.assertTrue(masked.endswith(tail))
            self.assertEqual(writes, {'i','chosen'})
            self.assertEqual(isolate_counted_loops(body, allow_early_exit=False), (body, set()))
        returning = loop.replace('EXIT','RETURN') + tail
        self.assertEqual(isolate_counted_loops(returning), (returning, set()))
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
                              '<Variable name="Bay"><Type><TypeName>INT</TypeName></Type></Variable>'
                              '</GlobalVars><Program name="P"><BodyContent><ST>'+loop+tail+
                              '</ST></BodyContent></Program></Project>')
        self.assertIn('bay', scope_selection(inv,None)[0])

    def test_counted_loop_writes_only_alias_their_storage_region(self):
        from plc_trace.capture.step_discovery import isolate_counted_loops, loop_may_write
        code = ('FOR I:=2 TO 1 BY -1 DO Cell.Motor[I].Move:=TRUE; Cell.Lanes[1,2].Value:=I; '
                'IF Sensor[I] THEN EXIT; END_IF; END_FOR; ')
        _, touched = isolate_counted_loops(code)
        for path in ('Cell', 'Cell.Motor', 'Cell.Motor[1].Move', 'Cell.Lanes[1][2]', 'Cell.Lanes[1,2].Value'):
            self.assertTrue(loop_may_write(path, touched), path)
        for path in ('Cell.Phase','Cell.MotorTest','Cell.Lanes[1,3].Value','Other.Motor[1].Move'):
            self.assertFalse(loop_may_write(path, touched), path)
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
                              '<Variable name="Cell"><Type><TypeName>Unit</TypeName></Type></Variable>'
                              '</GlobalVars><Program name="P"><BodyContent><ST>'+code+
                              'CASE Cell.Phase OF 170: IF Scanned THEN Cell.Phase:=900; END_IF; '
                              '900: Cell.Phase:=170; END_CASE;</ST></BodyContent></Program></Project>')
        row = scope_selection(inv,None)[0]['cell.phase']
        self.assertIn((170,900), {(t['from'],t['to']) for t in row['transitions']})

    def test_plcopen_launch_and_completion_do_not_reverse_or_include_bypasses(self):
        variables = ('Main', 'Worker', 'Decoy')
        body = ('CASE Main OF 1: Worker:=10; Main:=2; '
                '2: IF Worker=30 AND (Decoy=1 OR Skip) THEN Main:=1; END_IF; END_CASE; '
                'CASE Worker OF 10: IF Ready THEN Worker:=30; END_IF; 30: Worker:=0; END_CASE; '
                'CASE Decoy OF 1: Decoy:=2; 2: Decoy:=1; END_CASE;')
        xml = ('<project><fileHeader productName="InoProShop"/><globalVars>'
               +''.join(f'<variable name="{n}"><type><INT/></type></variable>' for n in variables)
               +'</globalVars>'+program('Site','<body><ST>'+body+'</ST></body>')
               +'<resource><task name="T"><pouInstance name="Site"/></task></resource></project>')
        _, inventory = self.prepare(xml)
        rows = {r['name']:r for r in inventory['candidates']}
        self.assertEqual({(link['kind'],link['parent'],link['child']) for link in rows['Main']['control_links']},
                         {('starts_child','Main','Worker'), ('waits_for_child','Main','Worker')})
        self.assertEqual(rows['Decoy']['control_links'], [])

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

    def test_nested_and_parallel_sequences_remain_capture_channels(self):
        control = ('CASE AxisX OF 2: IF Ready THEN AxisX:=9; END_IF; '
                   '9: IF ExitOK THEN AxisX:=2; END_IF; END_CASE; '
                   'IF AxisX=2 THEN CASE Ticket OF 1: IF Ready THEN Ticket:=5; END_IF; 5: Ticket:=1; END_CASE; END_IF; '
                   'CASE Lane OF 4: IF AxisX=2 OR BufferFree THEN Lane:=6; END_IF; '
                   '6: Lane:=4; END_CASE;')
        decl = ''.join(f'<Variable name="{n}"><Type><TypeName>INT</TypeName></Type></Variable>'
                       for n in ('AxisX', 'Lane', 'Ticket'))
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+decl+
                              '</GlobalVars><Program name="Bay"><BodyContent name="Cycle"><ST>'+control+
                              '</ST></BodyContent></Program></Project>')
        result = self.finish(inv, {'AxisX', 'Lane', 'Ticket'})
        self.assertEqual({c['name'] for c in result['channels']}, {'AxisX','Lane','Ticket'})
        self.assertEqual(result['scope_uncertain'], 0)
        decision = json.loads((self.out/'scope_results.json').read_bytes())['decisions']
        self.assertEqual(next(r for r in decision if r['name']=='Ticket')['gated_by'], ['axisx'])
        self.assertEqual(result['status'], 'done')
        with Path(result['configuration']).open(encoding='utf-8-sig', newline='') as f:
            header = next(r for r in csv.reader(f) if r and r[0] == 'Index')
        self.assertEqual(header[5:], ['AxisX:INT','Lane:INT','Ticket:INT'])

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
        item.update(role='component_sequence', basis=['d2','t1'], use='production',
                    timing_basis=timing_basis('工装内部交接'), boundaries=[{'action':'工装内部交接', 'write':'t1', 'via':'d2'}])
        chosen, reviewed = scope_selection(inv, scope)
        self.assertEqual(set(chosen), {'cursor[0]', 'cursor[1]'})
        self.assertEqual(reviewed[1]['admission'], 'captured')
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
        result = self.finish(inv, {'Head.Mark'})
        self.assertEqual({c['name'] for c in result['channels']}, {'Head.Mark'})
        self.assertEqual(result['retained_variables'], [])
        self.assertIn('Drive.Mark', (self.out / 'optional_variables.csv').read_text(encoding='utf-8-sig'))

    def test_registered_slot_in_own_unit_with_foreign_array_and_program(self):
        var = '<Variable name="{}"><Type><TypeName>ARRAY[0..5] OF INT</TypeName></Type></Variable>'
        def cycle(member):
            return f'CASE {member} OF 2: IF Ready THEN {member}:=7; END_IF; 7: {member}:=2; END_CASE;'
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
        self.assertIn('part[1]', selected)
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

    def test_sysmac_command_instances_and_auxiliary_cycle_survive_role_review_and_missing_types(self):
        control = ('<FunctionBlock name="Worker"><InOutVars><Variable name="part"><Type><TypeName>Unit</TypeName>'
                   '</Type></Variable></InOutVars><BodyContent name="Command"><ST>IF part.Mode=4 THEN '
                   'CASE part.Position OF 8: IF Arrived THEN part.Position:=15; END_IF; 15: part.Position:=8; END_CASE; END_IF;'
                   '</ST></BodyContent></FunctionBlock>')
        def variable(name, kind):
            return f'<Variable name="{name}"><Type><TypeName>{kind}</TypeName></Type></Variable>'
        xml = ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+variable('A','Unit')+
               variable('B','Unit')+variable('Pack','INT')+variable('Warm','INT')+
               '<Variable name="Reply"><Documentation>通信应答码</Documentation><Type><TypeName>INT</TypeName></Type></Variable>'+
               '<Variable name="Service"><Documentation>维护模式</Documentation><Type><TypeName>BOOL</TypeName></Type></Variable>'+
               '</GlobalVars>'+control+
               '<Program name="P"><LocalVars>'+variable('drive1','Worker')+variable('drive2','Worker')+
               '</LocalVars><BodyContent name="Cycle"><ST>drive1(part:=A); drive2(part:=B); '
               'IF Service THEN CASE Warm OF 0: Warm:=5; 5: Warm:=0; END_CASE; END_IF; '
               'CASE Pack OF 0: IF Done THEN Pack:=5; END_IF; 5: Pack:=0; END_CASE; '
               'IF Reply=1 THEN Ack:=TRUE; Reply:=0; END_IF;</ST></BodyContent></Program></Project>')
        first, inv = self.prepare(xml)
        expected = {'A.Position','B.Position','Pack'}
        self.assertEqual(first['channels'], [])
        self.assertEqual({r['name'] for r in scope_selection(inv, None)[0].values()}, expected | {'Warm'})
        scope = self.scope(inv, set())
        del scope['station_families']
        for row in scope['decisions']:
            row['role'] = 'auxiliary' if row['name'] == 'Warm' else 'component_sequence'
            if row['name'] == 'Reply':
                row.update(role='non_sequence', basis=['t1','e1'], reason='确认上位机写入的应答并清零')
            elif row['name'] == 'Warm':
                row.update(use='service', reason='上电准备步骤')
            else:
                signal = 'Done' if row['name'] == 'Pack' else 'Arrived'
                row.update(use='production', timing_basis=timing_basis('等待工件到位'), boundaries=[{'action':'等待工件到位','write':'t1','via':'signal:'+signal}])
        path = self.root/'review.json'
        path.write_text(json.dumps(scope))
        result = generate(self.source, self.out, scope_results=path)
        self.assertEqual(set((self.out/'static_variables.txt').read_text().splitlines()), expected)
        self.assertEqual((result['configured_variables'], result['configuration_complete'], result['status']),
                         (1, False, 'needs_review'))
        self.assertEqual(set(result['unresolved_types']), {'A.Position','B.Position'})
        config = Path(result['configuration'])
        self.assertTrue(config.name.endswith('_partial.csv'))
        with config.open(encoding='utf-8-sig', newline='') as f:
            self.assertEqual(next(r[5:] for r in csv.reader(f) if r[:1] == ['Index']), ['Pack:INT'])
        with (self.out/'capture_context.csv').open(encoding='utf-8-sig', newline='') as f:
            contexts = {(r['步序变量'],r['外层执行条件']) for r in csv.DictReader(f)}
        self.assertEqual(contexts, {('A.Position','A.Mode=4'), ('B.Position','B.Mode=4'), ('Pack','')})
        with (self.out/'optional_variables.csv').open(encoding='utf-8-sig', newline='') as f:
            self.assertEqual([(r['步序变量'],r['用途']) for r in csv.DictReader(f)], [('Warm','设备服务')])
        self.assertEqual(generate(self.source, self.out), result)

    def test_production_witness_is_bound_to_the_command_and_exit(self):
        code = ('IF Mode=1 THEN CASE Phase OF 10: ResetAxis:=TRUE; IF HomeOK THEN Phase:=20; END_IF; '
                '20: Phase:=10; END_CASE; END_IF; IF Mode=3 THEN CASE Phase OF 10: MoveAxis:=TRUE; '
                'IF AtPart THEN Phase:=20; END_IF; 20: Phase:=10; END_CASE; END_IF;')
        _, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'
                              '<Variable name="Phase"><Type><TypeName>INT</TypeName></Type></Variable>'
                              '</GlobalVars><Program name="P"><BodyContent><ST>'+code+
                              '</ST></BodyContent></Program></Project>')
        row = next(r for r in inv['candidates'] if r['name']=='Phase')
        write = next(t for t in row['transitions'] if 'AtPart' in t['guard'])
        home = next(e for e in row['effects'] if e['target']=='ResetAxis')
        move = next(e for e in row['effects'] if e['target']=='MoveAxis')
        scope = self.scope(inv, set())
        item = next(d for d in scope['decisions'] if d['name']=='Phase')
        proof = dict(action='移至取料位', write=write['id'], via=move['id'])
        item.update(role='component_sequence', use='production', timing_basis=timing_basis('工件移送',write['id']), boundaries=[proof])
        for bad in (home['id'], 'signal:Mode', 'signal:Phase', 'signal:HomeOK', 'e999'):
            with self.subTest(via=bad), self.assertRaisesRegex(ValueError,'佐证'):
                scope_selection(inv, {**scope, 'decisions':[{**item,'boundaries':[{**proof,'via':bad}]}]})
        for changes in ({'use':'service'}, {'boundaries':[]}, {'boundaries':[proof, proof]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                scope_selection(inv, {**scope, 'decisions':[{**item, **changes}]})
        path = self.root/'scope.json'
        path.write_text(json.dumps(scope))
        result = generate(self.source, self.out, scope_results=path)
        self.assertEqual(result['configured_variables'], 1)
        with (self.out/'capture_context.csv').open(encoding='utf-8-sig', newline='') as f:
            boundary, = csv.DictReader(f)
        self.assertEqual((boundary['外层执行条件'], boundary['生产动作或等待']), ('Mode=3','移至取料位'))
        self.assertIn('AtPart', boundary['写入条件'])

    def test_same_fb_type_can_serve_production_or_maintenance_in_distinct_instances(self):
        def var(name, typ):
            return f'<Variable name="{name}"><Type><TypeName>{typ}</TypeName></Type></Variable>'
        xml = ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars><Variable name="SetupMode">'+
               '<Documentation>设备维护模式</Documentation><Type><TypeName>BOOL</TypeName></Type></Variable></GlobalVars>'+
               '<FunctionBlock name="Unit"><Vars>'+
               var('Cursor','INT')+'</Vars><BodyContent><ST>CASE Cursor OF 2: IF Ready THEN Cursor:=5; '
               'END_IF; 5: Cursor:=2; END_CASE;</ST></BodyContent></FunctionBlock><Program name="P"><Vars>'+
               var('Cell','Unit')+var('Bench','Unit')+'</Vars><BodyContent><ST>IF Making THEN Cell(); '
               'END_IF; IF SetupMode THEN Bench(); END_IF;</ST></BodyContent></Program></Project>')
        _, inv = self.prepare(xml)
        scope = self.scope(inv, set())
        for item in scope['decisions']:
            item.update(role='component_sequence')
            if item['name']=='P.Cell.Cursor':
                item.update(use='production', timing_basis=timing_basis('等待工件就绪'), boundaries=[dict(action='等待工件就绪',write='t1',via='signal:Ready')])
            else:
                item.update(use='service',reason='校验台由设置模式启动')
        path = self.root/'roles.json'
        path.write_text(json.dumps(scope))
        result = generate(self.source,self.out,scope_results=path)
        self.assertEqual([r['name'] for r in result['channels']], ['P.Cell.Cursor'])
        with (self.out/'optional_variables.csv').open(encoding='utf-8-sig',newline='') as f:
            self.assertEqual([r['步序变量'] for r in csv.DictReader(f)], ['P.Bench.Cursor'])

    def test_feedback_proof_uses_only_parsed_operands(self):
        from plc_trace.capture.capture_use import timing_use
        source = Discovery()
        cursor = source.candidate('X')
        cursor.edge(1, 2, {'scope':'Cell','guard':"X=1 AND (Received OR @condition(Message='Arrived'))"})
        row = cursor.record()
        def accept(signal):
            return timing_use(row,dict(use='production',timing_basis=timing_basis('接收工件结果'),boundaries=[
                dict(action='接收工件结果',write='t1',via='signal:'+signal)]), True)
        self.assertEqual(accept('Received')['admission'], 'captured')
        for word in ('Arrived','Message','X','condition'):
            with self.subTest(word=word), self.assertRaises(ValueError):
                accept(word)

    def test_evidenced_data_exclusion_and_short_if_sequence(self):
        xml = ('<project><fileHeader productName="InoProShop"/><globalVars>'+
               ''.join(f'<variable name="{n}"><type><INT/></type><documentation>{note}</documentation></variable>'
                       for n,note in [('Recipe','产品编码'),('Short','单元出站步骤'),('One','单元回零步骤')])+
               '</globalVars>'+program('Line','<body><ST>CASE Recipe OF 1: Recipe:=2; 2: Recipe:=1; END_CASE; '
               'IF Short=4 AND Ready THEN Short:=7; END_IF; CASE One OF 9: One:=11; END_CASE;</ST></body>')+
               '<resource><task name="T"><pouInstance name="Line"/></task></resource></project>')
        first, inv = self.prepare(xml)
        self.assertEqual({c['name'] for c in scope_selection(inv, None)[0].values()}, {'One','Recipe'})
        self.assertEqual(first['channels'], [])
        short = next(r for r in inv['candidates'] if r['name']=='Short')
        self.assertEqual(short['eligibility'], 'single_write')
        scope = self.scope(inv, set())
        recipe = next(d for d in scope['decisions'] if d['name']=='Recipe')
        recipe.update(role='non_sequence', reason='编码在两个产品值间切换')
        with self.assertRaisesRegex(ValueError, '补充'):
            scope_selection(inv, scope)
        recipe['basis'].append('d1')
        scope['decisions'].append(dict(name='Short', role='component_sequence', basis=['d1','t1'],
                                       reason='声明为单元出站阶段，由到位条件推进'))
        chosen, _ = scope_selection(inv, scope)
        self.assertEqual(set(chosen), {'one','short'})

    def test_connected_values_in_different_fb_instances_do_not_form_a_sequence_witness(self):
        model = Discovery()
        for old, new, instance in [(1,2,'A'), (2,1,'B')]:
            model.candidate('Word').edge(old, new, {'scope':'F::Run', 'source_line':12,
                'binding':[{'scope':'P','instance':instance,'source_line':4}]})
        inv = model.inventory('source','unit')
        self.assertIsNone(inv['candidates'][0]['sequence_witness'])
        self.assertFalse(scope_selection(inv,None)[0])

    def test_sysmac_nested_instance_storage_and_wired_call(self):
        def var(name, typ):
            return f'<Variable name="{name}"><Type><TypeName>{typ}</TypeName></Type></Variable>'
        private = '<Vars>'+var('Phase','INT')+'</Vars><TempVars>'+var('Scratch','INT')+'</TempVars>'
        code = 'CASE Phase OF 3: Phase:=7; 7: Phase:=3; END_CASE; CASE Scratch OF 4: Scratch:=5; 5: Scratch:=4; END_CASE;'
        wired = ('<BodyContent name="R"><Rung><FbdObject xsi:type="Block" typeName="Worker" instanceName="Rail"/></Rung>'
                 '</BodyContent>')
        xml = ('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
               '<FunctionBlock name="Worker">'+private+'<BodyContent><ST>'+code+'</ST></BodyContent></FunctionBlock>'
               '<FunctionBlock name="Wrapper"><Vars>'+var('Inner','Worker')+'</Vars><BodyContent><ST>Inner();</ST>'
               '</BodyContent></FunctionBlock><Program name="P"><Vars>'+var('Cell','Wrapper')+var('Rail','Worker')+
               var('Idle','Worker')+'</Vars><BodyContent><ST>Cell(); IF FALSE THEN Idle(); END_IF;</ST></BodyContent>'+
               wired+'</Program></Project>')
        result, inv = self.prepare(xml)
        self.assertEqual({(c['name'],c['type']) for c in scope_selection(inv,None)[0].values()},
                         {('P.Cell.Inner.Phase','INT'),('P.Rail.Phase','INT')})
        rows = {r['name']:r for r in inv['candidates']}
        self.assertEqual([c['instance'] for c in rows['P.Cell.Inner.Phase']['transitions'][0]['binding']], ['Cell','Inner'])
        self.assertEqual([s['name'] for s in rows['P.Cell.Inner.Phase']['declarations']], ['Cell','Inner','Phase'])

    def test_plcopen_wired_nested_instance_members_and_program_qualifiers(self):
        def decl(names, typ):
            return '<interface><localVars>'+''.join(f'<variable name="{n}"><type>{typ}</type></variable>' for n in names)+'</localVars></interface>'
        def call(n, kind, instance):
            return f'<block localId="{n}" typeName="{kind}" instanceName="{instance}"><inputVariables>'+port('EN',0)+'</inputVariables></block>'
        child = ('<pou name="Worker" pouType="functionBlock">'+decl(['Cursor'],'<INT/>')+
                 '<body><ST>CASE Cursor OF 7: IF Ready THEN Cursor:=9; END_IF; 9: Cursor:=7; END_CASE;</ST></body></pou>')
        parent = ('<pou name="Wrapper" pouType="functionBlock">'+decl(['Held'],'<derived name="Worker"/>')+
                  ladder(call(6,'Worker','Held'))+'</pou>')
        body = decl(['A','B','Idle'],'<derived name="Wrapper"/>')+ladder(call(4,'Wrapper','A')+call(5,'Wrapper','B'))
        xml = ('<project><fileHeader productName="InoProShop"/>'+child+parent+program('Line',body)+
               '<resource><task name="Clock"><pouInstance name="Line"/></task></resource></project>')
        first, inv = self.prepare(xml)
        self.assertEqual({c['name'] for c in scope_selection(inv,None)[0].values()}, {'Line.A.Held.Cursor','Line.B.Held.Cursor'})
        result = self.finish(inv,{'Line.A.Held.Cursor','Line.B.Held.Cursor'})
        self.assertTrue(result['configuration_complete'])
        self.assertEqual(result['configured_variables'],2)
        row = next(r for r in inv['candidates'] if r['name']=='Line.B.Held.Cursor')
        self.assertEqual([s['name'] for s in row['declarations']], ['B','Held','Cursor'])

    def test_dedicated_array_references_preserve_exact_storage_and_bounds(self):
        array = lambda n: (f'<Variable name="{n}"><Documentation>单元步序槽</Documentation>'
                           '<Type><TypeName>ARRAY[0..3] OF INT</TypeName></Type></Variable>')
        code = ('CASE A[0] OF 1: IF Ready THEN A[0]:=2; END_IF; 2: A[0]:=1; END_CASE; '
                'COPY:=A[2]; Other:=B[2]; A[6]:=0;')
        first, inv = self.prepare('<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>'+array('A')+
                                  array('B')+'</GlobalVars><Program name="P"><BodyContent><ST>'+code+
                                  '</ST></BodyContent></Program></Project>')
        self.assertEqual((first['static_variables'], first['sequence_variables']), (0,1))
        scope = self.scope(inv, {'A[0]'})
        scope['decisions'][0]['dedicated_step_array'] = True
        invalid = copy.deepcopy(scope)
        invalid['decisions'][0]['boundaries'][0]['via'] = 'd1'
        with self.assertRaisesRegex(ValueError, '佐证'):
            scope_selection(inv, invalid)
        path = self.root/'scopes.json'
        path.write_text(json.dumps(scope))
        final = generate(self.source, self.out, scope_results=path)
        self.assertEqual((final['sequence_variables'],final['referenced_step_slots'],final['configured_variables']), (1,1,1))
        self.assertEqual({c['name'] for c in final['channels']}, {'A[0]'})
        with (self.out/'optional_variables.csv').open(encoding='utf-8-sig', newline='') as f:
            self.assertEqual([(r['步序变量'],r['用途']) for r in csv.DictReader(f)], [('A[2]','仅固定引用')])
        group, = json.loads((self.out/'step_variable_discovery.json').read_bytes())['step_arrays']
        self.assertEqual((group['array'],group['bounds'],group['referenced_slots']), ('A',[0,3],['A[2]']))
