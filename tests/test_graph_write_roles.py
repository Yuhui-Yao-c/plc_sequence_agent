import shutil
import subprocess
import unittest
from pathlib import Path

from plc_trace.graph.dependencies import WireSlice, signal_dependencies
from plc_trace.graph.topology import st_body


class WriteRoleTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the offline matrix test')
    def test_program_matrix_direction_and_receiver_observations(self):
        subprocess.run(['node', str(Path(__file__).with_name('graph_overview.cjs'))], check=True)

    def network(self):
        def node(i, typ, label, **extra):
            return dict(id=i, type=typ, label=label, **extra)
        nodes = [
            node(0, 'leftPowerRail', 'rail'), node(1, 'contact', 'Enable', operands=[['read', 'Enable']]),
            node(2, 'inVariable', 'Quality[2,1]'), node(3, 'inVariable', 'Limit'),
            node(4, 'block', 'EQ', main_output=''), node(5, 'inVariable', 'SourceValue'),
            node(6, 'block', 'MOVE', operands=[['write', 'Cursor', 'OUT']]),
        ]
        wires = [[0,'',1,'','control'], [1,'',4,'EN','control'], [2,'',4,'IN1','pin'],
                 [3,'',4,'IN2','pin'], [4,'',6,'EN','control'], [5,'',6,'IN','pin']]
        return {'id':0, 'scope':'Stage', 'language':'LD', 'nodes':nodes, 'wires':wires}

    def test_comparison_main_result_and_eno_take_different_routes(self):
        net = self.network()
        inputs, stops = WireSlice([net], []).trace(net['nodes'][6])
        self.assertEqual(inputs, {'when':{'Enable','Quality[2,1]','Limit'},'value':{'SourceValue'}})
        self.assertFalse(stops)
        net['wires'][-2][1] = 'ENO'
        inputs, stops = WireSlice([net], []).trace(net['nodes'][6])
        self.assertEqual(inputs, {'when':{'Enable'}, 'value':{'SourceValue'}})
        self.assertFalse(stops)

    def test_enabled_producer_feeds_data_without_enabling_the_destination(self):
        net = self.network()
        net['wires'][-2:] = [[4,'',6,'IN','pin']]
        inputs, stops = WireSlice([net], []).trace(net['nodes'][6])
        self.assertEqual(inputs, {'when':set(),'value':{'Quality[2,1]','Limit'},'value_enable':{'Enable'}})
        self.assertFalse(stops)
        net['wires'][-1][1] = 'ENO'
        inputs, stops = WireSlice([net], []).trace(net['nodes'][6])
        self.assertEqual(inputs, {'when':set(),'value':{'Enable'}})

    def test_unknown_or_ambiguous_output_is_an_interface_boundary(self):
        net = self.network()
        net['nodes'][4]['ambiguous_output'] = True
        inputs, stops = WireSlice([net], []).trace(net['nodes'][6])
        self.assertEqual(inputs['when'], set())
        self.assertIn((4, '', 'ambiguous_port'), stops)
        del net['nodes'][4]['ambiguous_output']
        inputs, stops = WireSlice([net], [{'node':4,'st_line':0}]).trace(net['nodes'][6])
        self.assertEqual(inputs['when'], set())
        self.assertIn((4, '', 'block_output'), stops)

    def test_only_real_wires_supply_dependencies(self):
        net = self.network()
        names = ['Enable','Quality[2,1]','Limit','SourceValue','Cursor','Elsewhere']
        ids = {('global',k.casefold()):i for i,k in enumerate(names)}
        site = {'source_node':6,'net':0,'channel':'Cursor','signal_refs':[['Elsewhere',5]]}
        roles = []
        deps = signal_dependencies([net],ids,lambda scope,v:'global',set(),sites=[site],roles=roles)
        self.assertEqual(deps, [[4,[0,1,2,3],0,6,'wire',0]])
        self.assertEqual(roles, [{'when':[0,1,2],'value':[3],'sites':[0]}])

    def test_embedded_st_retains_the_external_enable_and_same_line_statement(self):
        net = self.network()
        n = {'id':7,'type':'InlineST','label':'code'}
        st_body(n,'IF LocalGate THEN First:=Hot AND Ready; Second:=Cool; END_IF;')
        net['nodes'].append(n)
        net['wires'].append([1,'',7,'EN','control'])
        names = ['Enable','LocalGate','First','Second','Hot','Ready','Cool']
        ids = {('global',v.casefold()):i for i,v in enumerate(names)}
        roles = []
        deps = signal_dependencies([net], ids, lambda _,v:'global', set(), roles=roles)
        self.assertEqual([row[0] for row in deps],[2,3])
        self.assertEqual([r['when'] for r in roles],[[0,1],[0,1]])
        self.assertEqual([r['value'] for r in roles],[[4,5],[6]])
        self.assertEqual([r['enable'] for r in roles],[[0],[0]])
        self.assertEqual([r['statement'] for r in roles],[0,1])

    def test_ordinary_and_latched_coils_use_the_rung_differently(self):
        net = self.network()
        for i, mode in [(7,'none'),(8,'set')]:
            net['nodes'].append({'id':i,'type':'coil','label':'Output','storage':mode,
                                 'operands':[['write',f'Coil{i}']]})
            net['wires'].append([1,'',i,'','control'])
        ids = {('global',v):i for i,v in enumerate(['enable','coil7','coil8'])}
        roles = []
        signal_dependencies([net],ids,lambda _,v:'global',set(),roles=roles)
        self.assertEqual(roles,[{'when':[],'value':[0]},{'when':[0],'value':[]}])
