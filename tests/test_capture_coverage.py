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
                   'effects':[{'step':7,'global_target':'dev.done','value':'TRUE'}]}
            return _covered_transaction(child,_job_ports([parent]))
        self.assertEqual(proof()['channel'],'Phase')
        self.assertIsNone(proof('Phase=4 AND (Dev.Done OR Bypass)'))
        self.assertIsNone(proof('Phase=4 AND NOT Dev.Done'))
        self.assertIsNone(proof(mode='Mode=2'))
        self.assertIsNone(proof(extra=True))
        self.assertIsNone(proof(request='other.go'))
        self.assertIsNone(proof(outputs=['axis.target']))
        self.assertIsNone(proof(trigger='OtherTrigger'))
