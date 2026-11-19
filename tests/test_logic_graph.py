import argparse
import csv
import gzip
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from plc_trace.cli import _expand_trace_shorthand
from plc_trace.graph.model import build
from plc_trace.graph.topology import Topology
from plc_trace.graph.workflow import run
from tests.test_plcopen_trace_control import contact, ladder, move, operand, port, program


def record(*channels):
    return '<TraceRecord>'+''.join(
        f'<TraceVariable VarName="{name}"><Values>{values}</Values><Timestamps>{times}</Timestamps></TraceVariable>'
        for name,values,times in channels)+'</TraceRecord>'


def declarations(*names):
    return '<globalVars>'+''.join(f'<variable name="{n}"><type><INT/></type></variable>' for n in names)+'</globalVars>'


class LogicGraphTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.src=self.root/'input.xml'
        self.trace=self.root/'run.trace'

    def plc(self, body, *globals_, other=''):
        self.src.write_text('<project>\n'+declarations(*globals_)+'\n'+program('Cell',body)+'\n'+other+'\n</project>')

    def model(self):
        return build(self.src,self.trace)

    def test_tree_parallel_inverted_and_integer_output_ports(self):
        nodes=(operand(1,'Cursor')+operand(2,'100')+
               '<block localId="3" typeName="EQ"><inputVariables>'+
               port('EN',0)+port('IN1',1)+port('IN2',2)+'</inputVariables></block>'+
               contact(4,[3],'Lift').replace('localId="4"','localId="4" negated="true"')+
               contact(5,[3],'Bypass')+contact(6,[4,5],'Run')+operand(7,'110')+move(8,6,7,'Cursor'))
        self.plc(ladder(nodes),'Cursor','Lift','Run','Bypass')
        self.trace.write_text(record(('Cursor','100,110','0,4'),('Lift','TRUE,FALSE','0,4')))
        d=self.model()
        c=next(c for c in d['channels'] if c['name']=='Cursor')
        e=c['pairs'][0];s=d['sites'][e['sites'][0]]
        self.assertEqual(e['relation'],'direct')
        self.assertEqual(e['events'][0][3],{'lift':0})
        self.assertIn('NOT (Lift)',s['guard']);self.assertIn(' OR ',s['guard'])
        net=d['nets'][s['net']];ids={n['key']:n['id'] for n in net['nodes']}
        self.assertIn([ids['6'],'',ids['8'],'EN','control'],net['wires'])
        self.assertEqual(s['source_node'],ids['8'])
        cursor=next(v for v in d['symbols'] if v['name']=='Cursor')
        self.assertTrue(cursor['writes']);self.assertTrue(cursor['reads'])

    def test_paths_ambiguous_parallel_writes_and_unobserved(self):
        code='''<body><ST>
CASE Cursor OF
  10: IF A THEN Cursor:=20; ELSIF B THEN Cursor:=20; END_IF;
  20: IF C THEN Cursor:=30; END_IF;
  30: IF Reset THEN Cursor:=10; END_IF;
END_CASE;</ST></body>'''
        self.plc(code,'Cursor','A','B','C','Reset',other=program('IdleOnly','<body><SFC/></body>'))
        self.trace.write_text(record(('Cursor','10,30,99','0,4,8')))
        d=self.model();states=next(x for x in d['channels'] if x['name']=='Cursor')
        pairs={(e['from'],e['to']):e for e in states['pairs']}
        self.assertEqual(len(pairs[10,20]['sites']),2)
        self.assertEqual(pairs[10,20]['relation'],'source_only')
        self.assertEqual(len(pairs[10,30]['paths'][0]),2);self.assertEqual(pairs[10,30]['relation'],'path')
        self.assertEqual(pairs[30,99]['relation'],'unresolved')
        self.assertTrue(any(x['scope']=='IdleOnly' for x in d['nets']))
        self.assertTrue(any(x['reason']=='unsupported_body_language' for x in d['wiring_issues']))

    def test_no_path_across_writers_no_suffix_merge_or_record_bridging(self):
        self.plc('<body><ST>IF K=1 AND R THEN K:=2; END_IF;</ST></body>','K','R',
                 other=program('Other','<body><ST>IF K=2 AND R THEN K:=3; END_IF;</ST></body>'))
        self.trace.write_text('<TraceData>'+record(('K','1,3','0,4'),('Panel.K','1,2','0,4'))+
                              record(('K','20,1','0,4'))+'</TraceData>')
        d=self.model();rows={x['name']:x for x in d['channels']}
        hits={(x['from'],x['to']):x for x in rows['K']['pairs'] if x.get('events')}
        self.assertEqual(set(hits),{(1,3),(20,1)})
        self.assertEqual(hits[1,3]['relation'],'unresolved')
        self.assertFalse(rows['Panel.K']['pairs'][0]['sites'])

    def test_duplicate_time_no_inferred_operand_and_gaps(self):
        self.plc('<body><ST>IF K=1 AND R THEN K:=2; END_IF;</ST></body>','K','R')
        self.trace.write_text(record(('K','1,2,1,2','0,4,100,104'),('R','0,1,0','0,4,4')))
        d=build(self.src,self.trace,4)
        e=next(x for c in d['channels'] if c['name']=='K' for x in c['pairs'] if x['to']==2)
        self.assertEqual(len(e['events']),2);self.assertTrue(all(len(x)==3 for x in e['events']))
        self.assertEqual(d['trace_audit']['operand_sample_limits']['no_unique_simultaneous_operand_sample'],2)

    def test_sysmac_bool_uses_same_row_and_recording(self):
        self.plc('<body><ST>IF K=1 AND Ready THEN K:=2; END_IF;</ST></body>','K','Ready')
        self.trace=self.root/'tables';self.trace.mkdir()
        for stamp, value in [('20260101120000','FALSE'),('20260102120000','TRUE')]:
            with (self.trace/(stamp+'_00001.csv')).open('w',newline='') as f:
                csv.writer(f).writerows([['TraceType','Continuous'],['Index','RawTime','K:INT','Ready:BOOL'],
                    [0,1700000000000000000,'1','TRUE'],[1,1700000000012000000,'2',value]])
        d=self.model();events=next(x for c in d['channels'] for x in c['pairs'] if x.get('events'))['events']
        self.assertEqual([(e[0],e[1],e[3]['ready']) for e in events],
                         [('20260101120000',1,0),('20260102120000',1,1)])

    def test_control_dependency_direction_and_guard_writer_lookup(self):
        self.plc('<body><ST>IF Peer=4 AND Ready THEN Peer:=5; END_IF; '
                 'IF Main=1 AND (Peer=5 OR Override) THEN Main:=2; END_IF; '
                 'IF Enable THEN Ready:=TRUE; END_IF;</ST></body>',
                 'Peer','Ready','Main','Override','Enable')
        self.trace.write_text(record(('Main','1,2','0,4')))
        d=self.model()
        link=next(x for x in d['channel_dependencies'] if x['read']=='Peer' and x['written']=='Main')
        self.assertTrue(all(' OR ' in d['sites'][i]['guard'] for i in link['sites']))
        signal=next(x for x in d['symbols'] if x['name']=='Ready')
        self.assertTrue(signal['writes']);self.assertTrue(signal['guard_sites'])

    def test_large_integer_observations_are_exact_in_the_browser_payload(self):
        self.plc('<body><ST>X:=1;</ST></body>','X')
        self.trace.write_text(record(('Wide','9007199254740992,9007199254740993','0,4')))
        data=self.model()
        edge=next(c for c in data['channels'] if c['name']=='Wide')['pairs'][0]
        self.assertEqual((edge['from'],edge['to']),('9007199254740992','9007199254740993'))

    def test_same_name_local_writers_and_calls_have_separate_owners(self):
        ctrl='<interface><localVars><variable name="Latch"><type><BOOL/></type></variable></localVars></interface>'
        dom=lambda name: program(name,ctrl+'<body><ST>IF Flag THEN Latch:=TRUE; END_IF; </ST></body>')
        main=ladder('<block localId="50" typeName="DriveFB" instanceName="drive"/>')
        self.plc(main,'Flag',other=dom('Left')+dom('Right')+
                 '<pou name="DriveFB" pouType="functionBlock"><body><ST>X:=1;</ST></body></pou>')
        self.trace.write_text(record(('Unobserved','0,1','0,4')))
        data=self.model()
        scopes={s['scope'] for s in data['symbols'] if s['name']=='Latch'}
        self.assertEqual(scopes,{'local:left','local:right'})
        self.assertTrue(any(c['resolved'] and c['targets']==['DriveFB'] for c in data['calls']))

    def test_port_resolution_is_body_local_and_ambiguous_ports_are_not_wires(self):
        bad=('<block localId="4" typeName="Opaque"><outputVariables><variable formalParameter="Q"/>'
             '</outputVariables></block><block localId="5" typeName="Use"><inputVariables>'+
             port('IN',4,'Other')+port('EN',9)+'</inputVariables></block>')
        self.plc(ladder(bad),other=program('Sibling',ladder(operand(9,'X')+operand(7,'Y')+operand(7,'Z')+contact(1,[7],'K'))))
        g=Topology();g.read(self.src,'plcopen')
        self.assertEqual({x['reason'] for x in g.issues},{'missing_source_id','duplicate_source_id','unknown_output_pin'})
        self.assertEqual(sum(len(n['wires']) for n in g.nets),0)

    def test_smc2_grid_invalid_shape_and_unbound_trace_are_explicit(self):
        nets=[{'CLs':[{'__type':'LD','X':0,'Y':0,'Var':'Flag'},
                      {'__type':'ST','X':1,'Y':0,'Var':'Motor'}]},
              {'CLs':[{'__type':'LD','X':0,'Y':0},{'__type':'ST','X':0,'Y':0}]}]
        self.src.write_text('<smc2><entry type="json" name="R">'+json.dumps(nets)+'</entry></smc2>')
        self.trace.write_text(record(('Unbound','4,8','0,4')))
        d=self.model()
        self.assertEqual(len(d['nets'][0]['wires']),1);self.assertEqual(len(d['nets'][1]['wires']),0)
        self.assertTrue(d['wiring_issues']);self.assertEqual(d['summary']['unresolved'],1)

    def test_routing_replay_and_corrupt_delivery(self):
        args=_expand_trace_shorthand(argparse.ArgumentParser(),['a.xml','b.trace','--logic-graph','-output','work'])
        self.assertEqual(args,['logic-graph','--source=a.xml','--trace=b.trace','-output=work'])
        self.plc('<body><ST>IF K=1 THEN K:=2; END_IF;</ST></body>','K')
        self.trace.write_text(record(('K','1,2','0,4')))
        a=SimpleNamespace(source=self.src,trace=self.trace,out=self.root/'out',output_root=None,sample_interval_ms=None)
        with redirect_stdout(io.StringIO()):
            first=run(a)
            with patch('plc_trace.graph.workflow.build',side_effect=AssertionError('rebuilt')):
                self.assertEqual(first,run(a))
        with gzip.open(a.out/'logic_graph.json.gz','rt') as f:
            self.assertEqual(json.load(f)['schema'],'plc.logic_graph.v1')
        (a.out/'logic_graph.html').write_text('changed')
        with self.assertRaisesRegex(ValueError,'变化'):
            run(a)
