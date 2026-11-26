import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from openpyxl import Workbook

from plc_trace.capture.channel_reference import read_reference, verify_config
from plc_trace.capture.coverage import _covered_transaction, _job_ports
from plc_trace.capture.extract_step_variables import generate, _inovance_config, _sysmac_config
from plc_trace.capture.step_discovery import scope_selection


def sysmac(globals_, programs):
    decl = ''.join(f'<Variable name="{n}"><Type><TypeName>{t}</TypeName></Type></Variable>' for n,t in globals_)
    bodies = ''.join(f'<Program name="{n}"><BodyContent><ST>{s}</ST></BodyContent></Program>' for n,s in programs)
    return f'<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10"><GlobalVars>{decl}</GlobalVars>{bodies}</Project>'


def neutral(inv):
    return {'inventory_sha256': inv['inventory_sha256'], 'decisions': [
        {'name': r['name'], 'role':'uncertain','reason':'用途待核','basis':['t1']}
        for r in inv['candidates'] if r['eligibility'] == 'scope_required']}


class CoverageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.xml = self.root/'工序.xml'

    def prepare(self, xml, **options):
        self.xml.write_text(xml)
        out = self.root/'run'
        generate(self.xml, out, **options)
        return out, json.loads((out/'step_variable_discovery.json').read_bytes())

    def merge(self, out, scope, **options):
        reviews = self.root/'roles.json'
        reviews.write_text(json.dumps(scope))
        return generate(self.xml, out, scope_results=reviews, **options)

    def test_preview_and_reviewed_config_keep_untyped_targets_and_group_by_declared_owner(self):
        variables = [('Arm','Job'), ('Rack','ARRAY[1..3] OF Job'), ('Buffer','Transfer'), ('Known','INT')]
        names = ['Arm.Phase', 'Rack[2].Phase', 'Buffer.Phase', 'Known']
        body = ' '.join(f'CASE {n} OF 1: {n}:=4; 4: {n}:=1; END_CASE;' for n in names)
        out, inv = self.prepare(sysmac(variables, [('Cell',body)]))
        first = json.loads((out/'workflow_status.json').read_bytes())
        self.assertEqual((first['static_variables'], first['trace_target_count'], first['preview_configured_variables']),
                         (0, 4, 1))
        self.assertEqual(set(Path(first['trace_targets']).read_text().splitlines()), set(names))
        self.assertIn('Known:INT', Path(first['preview_configuration']).read_text(encoding='utf-8-sig'))
        final = self.merge(out, neutral(inv))
        self.assertEqual((final['status'],final['configured_variables'],final['static_variables']), ('needs_review',1,4))
        self.assertFalse(Path(first['preview_configuration']).exists())
        self.assertEqual(set(Path(final['trace_targets']).read_text().splitlines()), set(names))
        with Path(final['member_type_template']).open(encoding='utf-8-sig') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r['结构类型'],r['成员'],r['数据类型'],r['地址数']) for r in rows],
                         [('Job','Phase','','2'),('Transfer','Phase','','1')])
        supplement = self.root/'verified.csv'
        supplement.write_text('结构类型,成员,数据类型\nJob,Phase,UINT\nTransfer,Phase,DINT\n')
        complete = generate(self.xml,self.root/'completed',scope_results=out/'scope_results.json',member_types=supplement)
        self.assertTrue(complete['configuration_complete'])
        self.assertIsNone(complete['member_type_template'])
        self.assertEqual({r['name']:r['type'] for r in complete['channels']},
                         {'Arm.Phase':'UINT','Rack[2].Phase':'UINT','Buffer.Phase':'DINT','Known':'INT'})
        self.assertEqual((out/'workflow_status.json').read_bytes(),json.dumps(final,ensure_ascii=False,indent=2).encode())

    def test_reference_type_applies_only_to_instances_of_the_same_omitted_scalar_member(self):
        reference = self.root/'reference.csv'
        reference.write_text('变量,类型\nLeft.Stage,UINT\n')
        addresses = ['Left.Stage','Right.Stage','Other.Stage','Left.Index[1]']
        xml = sysmac([('Left','Outer'),('Right','Outer'),('Other','Inner')], [('Line',' '.join(
            f'CASE {n} OF 1: {n}:=2; 2: {n}:=1; END_CASE;' for n in addresses))])
        out, inv = self.prepare(xml,reference=reference)
        before = self.merge(out,neutral(inv),reference=reference)
        self.assertEqual({r['name']:r['type'] for r in before['channels']},
                         {'Left.Stage':'UINT','Right.Stage':'UINT','Other.Stage':None,'Left.Index[1]':None})
        reference.write_text('变量,类型\nLeft.Stage,UINT\nRight.Stage,DINT\n')
        with self.assertRaisesRegex(ValueError,'冲突'):
            generate(self.xml,self.root/'conflict',scope_results=out/'scope_results.json',reference=reference)
        self.assertFalse((self.root/'conflict/workflow_status.json').exists())

    def test_explicit_pattern_exports_known_subset_and_rejects_a_conflicting_real_declaration(self):
        xml = sysmac([('Leaf','Box'),('Port','LibraryPort')],[('Line','Leaf.Mark:=2; Port.Mark:=3; Port.Child.Mark:=4;')])
        xml = xml.replace('<GlobalVars>', '<DataTypeDecl name="Box"><Member name="Mark"><Type><TypeName>INT</TypeName>'
                          '</Type></Member></DataTypeDecl><GlobalVars>')
        self.xml.write_text(xml)
        st = generate(self.xml,self.root/'mixed',pattern='*.Mark')
        self.assertEqual((st['status'],st['static_variables'],st['configured_variables']), ('needs_review',3,1))
        self.assertIn('Leaf.Mark:INT', Path(st['configuration']).read_text(encoding='utf-8-sig'))
        verified = self.root/'typed.csv'
        verified.write_text('结构类型,成员,数据类型\nBox,Mark,DINT\n')
        with self.assertRaisesRegex(ValueError,'冲突'):
            generate(self.xml,self.root/'conflict',pattern='*.Mark',member_types=verified)
        verified.write_text('结构类型,成员,数据类型\nLibraryPort,Mark,UINT\n')
        partial = generate(self.xml,self.root/'partial',pattern='*.Mark',member_types=verified)
        self.assertEqual((partial['configured_variables'],set(partial['unresolved_types'])),(2,{'Port.Child.Mark'}))

    def test_all_untyped_still_produces_the_entire_target_list_in_both_stages(self):
        out, inv = self.prepare(sysmac([('Arm','Interface')],[('Work','CASE Arm.State OF 0: Arm.State:=3; '
                                                          '3: Arm.State:=0; END_CASE;')]))
        first = json.loads((out/'workflow_status.json').read_bytes())
        self.assertIsNone(first['preview_configuration'])
        self.assertEqual(Path(first['trace_targets']).read_text().splitlines(),['Arm.State'])
        final = self.merge(out,neutral(inv))
        self.assertIsNone(final['configuration'])
        self.assertEqual(Path(final['trace_targets']).read_text().splitlines(),['Arm.State'])
        self.assertEqual(final['trace_target_count'],1)

    def test_member_binding_uses_the_concrete_program_local_and_declared_array_bounds(self):
        xml = sysmac([], [('West','Cursor[1].Phase:=2; Cursor[7].Phase:=2;'),('East','Cursor[1].Phase:=3;')])
        for program, kind in [('West','PressJob'),('East','SortJob')]:
            start = f'<Program name="{program}">'
            xml = xml.replace(start, start + f'<LocalVars><Variable name="Cursor"><Type>'
                              f'<TypeName>ARRAY[0..2] OF {kind}</TypeName></Type></Variable></LocalVars>')
        self.xml.write_text(xml)
        types = self.root/'types.csv'
        types.write_text('结构类型,成员,数据类型\nPressJob,Phase,DINT\n')
        status = generate(self.xml,self.root/'local',pattern='*Cursor[*].Phase',member_types=types)
        self.assertEqual({r['name']:r['type'] for r in status['channels']},
                         {'West.Cursor[1].Phase':'DINT','East.Cursor[1].Phase':None,'West.Cursor[7].Phase':None})
        self.assertEqual(status['configured_variables'],1)

    def test_renamed_production_cursor_survives_each_negative_role(self):
        for name,owner in [('Amber','Bay'),('物件位置','左侧工艺')]:
            xml = sysmac([(name,'INT')], [(owner,f'CASE {name} OF 2: IF Ready THEN Grip:=TRUE; {name}:=5; END_IF; 5: {name}:=2; END_CASE;')])
            self.xml.write_text(xml)
            folder = self.root/owner
            generate(self.xml,folder)
            inv=json.loads((folder/'step_variable_discovery.json').read_bytes())
            for role,use in [('component_sequence','detail'),('component_sequence','service'),('non_sequence','unresolved')]:
                review=neutral(inv)
                review['decisions'][0].update(role=role,use=use,basis=['t1','t2'])
                _, decisions=scope_selection(inv,review)
                self.assertEqual([(a['name'],a['admission']) for a in decisions],[(name,'retained')])

    def test_short_array_and_external_start_survive_omitted_decisions(self):
        xml=sysmac([('Token','ARRAY[0..4] OF INT'),('Pin','INT'),('Limit','INT')],
                    [('Work','CASE Token[0] OF 5: Token[0]:=8; 8: Token[0]:=5; END_CASE; '
                      'IF Token[3]=0 AND Ready THEN Token[3]:=7; END_IF; '
                      'IF Pin=2 AND Grip THEN Pin:=9; END_IF; IF Limit=0 THEN Limit:=6; END_IF;'),
                     ('Launch','IF Load THEN Pin:=2; END_IF;')])
        out,inv=self.prepare(xml)
        status=self.merge(out,neutral(inv))
        self.assertEqual({r['name'] for r in status['channels']},{'Token[0]','Token[3]','Pin'})
        self.assertEqual(status['configured_variables'],3)
        self.assertFalse(status['coverage_complete'])
        with (out/'optional_variables.csv').open(encoding='utf-8-sig') as f:
            self.assertIn('Limit',{r['步序变量'] for r in csv.DictReader(f)})
        pin=next(r for r in inv['candidates'] if r['name']=='Pin')
        self.assertIn('Launch',pin['entry_writes'][0]['scope'])

    def test_distributed_writes_in_both_adapters_round_trip_without_inventing_a_transition(self):
        writer='IF BeginJob THEN Token:=4; END_IF; IF Finish THEN Token:=7; END_IF;'
        reader='CASE Token OF 4: Clamp:=TRUE; 7: Lift:=TRUE; END_CASE;'
        native=sysmac([('Token','INT')],[('Issue',writer),('Execute',reader)])
        plcopen=('<project><fileHeader productName="InoProShop"/><globalVars><variable name="Token"><type><INT/></type></variable></globalVars>'+
                 ''.join(f'<pou name="{p}" pouType="program"><body><ST>{x}</ST></body></pou>' for p,x in [('Issue',writer),('Execute',reader)])+
                 '<resource><task name="T"><pouInstance name="Issue"/><pouInstance name="Execute"/></task></resource></project>')
        for fmt,xml in [('native',native),('plcopen',plcopen)]:
            with self.subTest(fmt=fmt):
                self.xml.write_text(xml)
                out=self.root/fmt
                self.assertEqual(generate(self.xml,out)['status'],'scope_review_pending')
                inv=json.loads((out/'step_variable_discovery.json').read_bytes())
                row=next(r for r in inv['candidates'] if r['name']=='Token')
                self.assertEqual(row['eligibility'],'distributed_write')
                self.assertEqual(row['transitions'],[])
                for pair in row['dispatch_writes']:
                    self.assertIn('Issue',pair['write']['scope'])
                    self.assertIn('Execute',pair['dispatch']['scope'])
                s=self.merge(out,neutral(inv))
                self.assertEqual([a['name'] for a in s['channels']],['Token'])
                with (out/'capture_context.csv').open(encoding='utf-8-sig') as f:
                    self.assertEqual({(r['写前步值'],r['写后步值']) for r in csv.DictReader(f)},{('','4'),('','7')})
                replay=generate(self.xml,self.root/(fmt+'2'),scope_results=out/'scope_results.json')
                self.assertEqual(s['channels'],replay['channels'])

    def test_same_name_local_in_other_program_cannot_supply_dispatch_values(self):
        decl='<Vars><Variable name="Slot"><Type><TypeName>INT</TypeName></Type></Variable></Vars>'
        parts=[('A','IF Start THEN Slot:=1; END_IF; IF Done THEN Slot:=3; END_IF;'),
               ('B','CASE Slot OF 1: Clamp:=TRUE; 3: Lift:=TRUE; END_CASE;')]
        xml='<Project xmlns="www.iec.ch/public/TC65SC65BWG7TF10">'+''.join(
            f'<Program name="{n}">{decl}<BodyContent><ST>{s}</ST></BodyContent></Program>' for n,s in parts)+'</Project>'
        _,inv=self.prepare(xml)
        self.assertFalse(any(r.get('dispatch_writes') for r in inv['candidates']))

    def test_external_verified_types_and_missing_addresses_are_reported_separately(self):
        xml=sysmac([('A','OmittedType'),('ReadyPhase','INT')], [('Production',
           'CASE A.Cursor OF 4: A.Cursor:=7; 7: A.Cursor:=4; END_CASE; CASE ReadyPhase OF 1: ReadyPhase:=2; 2: ReadyPhase:=1; END_CASE;')])
        ref=self.root/'独立核验.csv'
        ref.write_text('变量,类型\nA.Cursor,DINT\nReadyPhase,INT\nMissing,INT\n')
        out,inv=self.prepare(xml,reference=ref)
        status=self.merge(out,neutral(inv),reference=ref)
        self.assertEqual(status['configured_variables'],2)
        self.assertTrue(status['configuration_complete'])
        self.assertFalse(status['coverage_complete'])
        self.assertEqual(status['status'],'needs_review')
        self.assertEqual(status['coverage']['reference_missing_from_config'],['Missing'])
        self.assertEqual(status['coverage']['reference_missing_from_list'],['Missing'])
        self.assertIn('A.Cursor:DINT',Path(status['configuration']).read_text(encoding='utf-8-sig'))
        ref.write_text('变量,类型\nReadyPhase,LREAL\n')
        with self.assertRaisesRegex(ValueError,'已有任务不同'):
            generate(self.xml,out,reference=ref)
        with self.assertRaisesRegex(ValueError,'类型.*冲突'):
            generate(self.xml,self.root/'conflict',scope_results=self.root/'roles.json',reference=ref)

    def test_manual_xlsx_distinguishes_cursor_from_unchanged_mode_and_ambiguous_rows(self):
        book=Workbook()
        book.active.append(('开始标签','结束标签'))
        book.active.append(('Mode=2 AND Head=3','Mode=2 AND Head=8'))
        book.active.append(('X=7','Y=8'))
        book.active.append(('Head=3',None))
        p=self.root/'核验.xlsx'
        book.save(p); book.close()
        names,types,gaps=read_reference(p,[])
        self.assertEqual(set(names),{'head'})
        self.assertEqual(types,{})
        self.assertEqual(gaps,[{'row':3,'operands':['x','y']}])

    def test_confirmed_read_only_channel_recovered_only_at_exact_declared_address(self):
        ref=self.root/'known.txt'
        ref.write_text('Phase\nRack[2]\nRack[8]\n')
        out,inv=self.prepare(sysmac([('Phase','INT'),('Rack','ARRAY[0..3] OF DINT')],
                [('Cell','CASE Phase OF 2: Phase:=7; 7: Phase:=2; END_CASE; Monitor:=Rack[2];')]),reference=ref)
        s=self.merge(out,neutral(inv),reference=ref)
        self.assertEqual({r['name'] for r in s['channels']},{'Phase','Rack[2]'})
        self.assertEqual(s['coverage']['reference_missing_from_config'],['Rack[8]'])
        recovered,=s['coverage']['reference_recovered']
        self.assertEqual((recovered['name'],recovered['type']),('Rack[2]','DINT'))
        with (out/'capture_context.csv').open(encoding='utf-8-sig') as f:
            r=next(r for r in csv.DictReader(f) if r['步序变量']=='Rack[2]')
        self.assertEqual((r['写前步值'],r['写后步值'],r['动作佐证']),('','','reference'))

    def test_parameter_default_and_counter_are_distinguished_from_a_motion_command(self):
        from plc_trace.capture.coverage import _data_use
        def row(effect, value, before, after, note=''):
            return dict(identity='word',name='Word',control_links=[],sequence_witness=False,
                        declarations=[{'path':'Word','text':note}],
                        transitions=[{'from':before,'to':after,'guard':f'Word={before}'}],
                        effects=[{'target':effect,'value':value}])
        self.assertIsNotNone(_data_use(row('Totals[4]','Totals[4] + 1',10,0)))
        self.assertIsNone(_data_use(row('Axis','TRUE',10,0)))
        self.assertIsNotNone(_data_use(row('Limits[3]','15',0,2,'累计时间设定')))
        self.assertIsNone(_data_use(row('Jaw','TRUE',0,2,'累计时间设定')))

    def test_dynamic_selector_is_reported_even_when_the_static_channel_is_configured(self):
        out,inv=self.prepare(sysmac([('A','INT'),('Rack','ARRAY[0..8] OF INT')],
                    [('Line','CASE A OF 2: A:=7; 7: A:=2; END_CASE; '
                             'CASE Rack[i] OF 1: Rack[i]:=2; 2: Rack[i]:=1; END_CASE;')]))
        s=self.merge(out,neutral(inv))
        self.assertIn('Line::ST::rack[i]',s['coverage']['source_limits']['unresolved_selectors'])
        self.assertTrue(s['configuration_complete'])
        self.assertFalse(s['coverage_complete'])

    def test_corrupt_serialization_cannot_replace_pending_run(self):
        xml=sysmac([('A','INT'),('B','INT')],[('P','CASE A OF 2: A:=5; 5: A:=2; END_CASE; CASE B OF 3: B:=7; 7: B:=3; END_CASE;')])
        out,inv=self.prepare(xml)
        before=(out/'workflow_status.json').read_bytes()
        for damaged in ('drop','rename','type','duplicate'):
            def writer(channels,task):
                raw=_sysmac_config(channels,task)
                return raw.replace(b',B:INT', {'drop':b'','rename':b',C:INT','type':b',B:SINT','duplicate':b',A:INT'}[damaged])
            with self.subTest(damaged=damaged), patch('plc_trace.capture.extract_step_variables._sysmac_config',writer):
                with self.assertRaisesRegex(ValueError,'配置回读'):
                    self.merge(out,neutral(inv))
                self.assertEqual((out/'workflow_status.json').read_bytes(),before)
        self.assertEqual(self.merge(out,neutral(inv))['configured_variables'],2)

    def test_native_trace_requires_sample_and_configuration_addresses_to_match(self):
        from plc_trace.paths import RESOURCE_ROOT
        template=RESOURCE_ROOT/'templates'/'inovance_trace.xml'
        channels=[{'name':'Phase'}]
        tree=ET.fromstring(_inovance_config(ET.parse(template).getroot(),channels,
                            {'task_name':'T','every_n_cycles':1,'buffer_entries':64},'0'*64))
        node=tree.find('./TraceData/TraceRecord/TraceVariable')
        node.set('VarName','Wrong')
        with self.assertRaisesRegex(ValueError,'采样地址'):
            verify_config(ET.tostring(tree),'inovance',channels)

    def test_parent_must_consume_exact_instance_result_in_same_mode(self):
        def site(i,a,b,guard,mode='Mode=1'):
            return {'id':i,'scope':'P','from':a,'to':b,'guard':guard,'guard_complete':True,
                    'operating_guard':mode,'global_reads':['dev.done','dev.go']}
        start=site('t1',3,4,'Phase=3 AND Mode=1')
        command={'id':'e1','scope':'P','step':3,'guard_complete':True,'operating_guard':'Mode=1','guard':'Phase=3 AND Mode=1','target':'Dev.Go','global_target':'dev.go','value':'TRUE'}
        def proof(guard='Phase=4 AND Dev.Done',mode='Mode=1',extra=False,request='dev.go',outputs=(),trigger='Dev.Go'):
            end=site('t2',4,8,guard,mode)
            parent={'identity':'phase','name':'Phase','effects':[command],'transitions':[start,end]}
            edges=[(0,7),(7,0)] if not extra else [(0,7),(7,9),(9,11),(11,0)]
            child={'identity':'p.helper.pos','request_inputs':[request],'control_links':[],
                   'transitions':[site(str(i),a,b,trigger if a==0 else 'Ready') for i,(a,b) in enumerate(edges)],
                   'other_outputs':outputs,
                   'effects':[{'step':7,'global_target':'dev.done','value':'TRUE','guard_complete':True}]}
            return _covered_transaction(child,_job_ports([parent]))
        self.assertEqual(proof()['channel'],'Phase')
        self.assertIsNone(proof('Phase=4 AND (Dev.Done OR Bypass)'))
        self.assertIsNone(proof('Phase=4 AND Dev.Done AND Other.Done'))
        self.assertIsNone(proof('Phase=4 AND Dev.Done AND ConveyorReady'))
        self.assertIsNone(proof('Phase=4 AND NOT Dev.Done'))
        self.assertIsNone(proof(mode='Mode=2'))
        self.assertIsNone(proof(extra=True))
        self.assertIsNone(proof(request='other.go'))
        self.assertIsNone(proof(outputs=['axis.target']))
        self.assertIsNone(proof(trigger='OtherTrigger'))
        self.assertIsNone(proof(trigger='Dev.Go OR IndependentCycle'))
