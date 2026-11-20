import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from plc_trace.graph.model import build
from tests.test_logic_graph import declarations, record
from tests.test_plcopen_trace_control import contact, ladder, operand, port, program


class PlantGraphTests(unittest.TestCase):
    def model(self, *parts):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, trace = root / 'plant.xml', root / 'test.trace'
            src.write_text('<project>' + '\n'.join(parts) + '</project>', encoding='utf-8')
            trace.write_text(record(('Phase', '10,20', '0,4')), encoding='utf-8')
            return build(src, trace)

    @staticmethod
    def st(code):
        return '<body><ST><![CDATA[' + code + ']]></ST></body>'

    def test_cooperation_uses_exact_scoped_access_and_includes_unobserved_code(self):
        locals_ = '<interface><localVars><variable name="Mine"><type><INT/></type></variable></localVars></interface>'
        a = program('Source', locals_ + self.st('Ticket[1]:=Flag; Mine:=Flag; Ticket[2]:=Flag;'))
        b = program('Receiver', locals_ + self.st(
            'IF Phase=10 AND Ticket[1]=2 AND Mine THEN Phase:=20; END_IF;'))
        c = program('Other', self.st('IF Mystery THEN Ticket[idx]:=7; END_IF;'))
        g = self.model(declarations('Phase', 'Ticket', 'Flag', 'Mine', 'idx'), a, b, c)
        p = g['plant']
        self.assertEqual(sorted(n for r in p['routines'] for n in r['nets']), list(range(len(g['nets']))))
        self.assertEqual(len(p['net_routine']), len(g['nets']))
        group = {v['scope']: i for i, v in enumerate(p['groups'])}
        buses = {g['symbols'][x['symbol']]['name']: x for x in p['signal_buses']}
        self.assertEqual(set(buses), {'Ticket[1]'})
        bus = buses['Ticket[1]']
        self.assertEqual(bus['writers'], [group['Source']])
        self.assertEqual(bus['guard_groups'], [group['Receiver']])
        self.assertTrue(all(g['sites'][i]['channel'] == 'Phase' for i in bus['guards']))
        local_mines = [s for s in g['symbols'] if s['name'] == 'Mine']
        self.assertEqual({s['scope'] for s in local_mines}, {'local:source', 'local:receiver'})
        self.assertIn('Other', group)

    def test_provenance_composes_through_a_real_statement_only(self):
        g = self.model(declarations('Phase','Input','Permit','Handoff','Unrelated','External'),
                       program('Sender', self.st('Permit:=Input;')),
                       program('Relay', self.st('IF Permit THEN Handoff:=TRUE; END_IF; Unrelated:=External;')),
                       program('Receiver', self.st('IF Phase=10 AND Handoff THEN Phase:=20; END_IF;')))
        edges = set()
        for dest, sources, net, node, reason, line in g['plant']['dependencies']:
            n = next(n for n in g['nets'][net]['nodes'] if n['id'] == node)
            self.assertIn(line, [a['line'] for a in n['assignments']])
            self.assertEqual(reason, 'st')
            edges.update((g['symbols'][s]['name'], g['symbols'][dest]['name']) for s in sources)
        self.assertTrue({('Input','Permit'),('Permit','Handoff'),('Handoff','Phase')} <= edges)
        self.assertNotIn(('Permit','Unrelated'), edges)
        self.assertNotIn(('External','Handoff'), edges)

    @unittest.skipUnless(shutil.which('node'), 'Node is needed only for the SVG rendering test')
    def test_ladder_symbols_ports_and_junctions(self):
        subprocess.run(['node', str(Path(__file__).with_name('ladder_symbols.cjs'))], check=True)

    def test_graphical_binding_is_per_call_and_opaque_interfaces_do_not_bridge(self):
        fb = '<pou name="Tool" pouType="functionBlock"/>'
        nodes = ''
        for i, value in enumerate(('Signal1','Signal2'),1):
            nodes += (operand(i,value) + f'<block localId="{10+i}" typeName="Tool" instanceName="i{i}">'
                      '<inputVariables>'+port('P', i)+'</inputVariables><outputVariables>'
                      '<variable formalParameter="Q"><connectionPointOut><expression>Out'+str(i)+
                      '</expression></connectionPointOut></variable></outputVariables></block>')
        g=self.model(declarations('Signal1','Signal2','Out1','Out2'),fb,program('Cell',ladder(nodes)))
        actuals=[]
        for i, ports in g['plant']['port_bindings']:
            call=g['calls'][i]
            node=next(n for n in g['nets'][call['net']]['nodes'] if n['id']==call['node'])
            actuals.append((node['instanceName'], sorted((pin,role,g['symbols'][s]['name']) for pin,role,s,_ in ports)))
        self.assertEqual(sorted(actuals), [(f'i{i}',[('P','read',f'Signal{i}'),('Q','write',f'Out{i}')]) for i in (1,2)])
        self.assertFalse(g['plant']['dependencies'])

    def test_two_calls_to_one_body_bind_to_distinct_runtime_signals(self):
        fb = ('<pou name="Gate" pouType="functionBlock"><interface>'
              '<inputVars><variable name="OK"><type><BOOL/></type></variable></inputVars>'
              '<outputVars><variable name="Allow"><type><BOOL/></type></variable></outputVars>'
              '</interface>' + self.st('Allow:=OK;') + '</pou>')
        local = ('<interface><localVars>' + ''.join('<variable name="'+i+'"><type><derived name="Gate"/>'
                 '</type></variable>' for i in ('first','second')) + '</localVars></interface>')
        caller = program('Owner', local + self.st(
            'first(OK:=RightIn, Allow=>RightOut); second(OK:=LeftIn, Allow=>LeftOut);'))
        g=self.model(declarations('Phase','RightIn','RightOut','LeftIn','LeftOut'), fb, caller)
        bridges = set()
        for dest, inputs, net, node, reason, line, cid in (row for row in g['plant']['dependencies'] if len(row) == 7):
            call=g['calls'][cid]
            self.assertEqual((g['nets'][net]['scope'], reason, line), ('Gate','instance_st',1))
            self.assertEqual(g['nets'][call['net']]['scope'],'Owner')
            bridges.update((g['symbols'][sid]['name'],g['symbols'][dest]['name'],call['instance']) for sid in inputs)
        self.assertEqual(bridges,{('RightIn','RightOut','first'),('LeftIn','LeftOut','second')})

    def test_native_vars_shadow_global_and_parameters_have_directions(self):
        from plc_trace.graph.topology import Topology
        doc = ('<Project><GlobalVars><Variable name="Shared"/></GlobalVars>'
               '<Program name="Feed"><Vars><Variable name="Shared"/></Vars></Program>'
               '<Program name="Reader"><ExternalVars><Variable name="Shared"/></ExternalVars></Program>'
               '<FunctionBlock name="Switch"><Parameters><InputVars><Variable name="Enable"/></InputVars>'
               '<InoutVars><Variable name="Status"/></InoutVars><OutputVars><Variable name="Done"/></OutputVars>'
               '</Parameters></FunctionBlock></Project>')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'scope.xml'
            path.write_text(doc)
            top = Topology()
            top.read(path, 'omron')
        self.assertEqual(top.namespace('Feed::Section','Shared'), 'local:feed')
        self.assertEqual(top.namespace('Reader::Section','Shared'), 'global')
        self.assertEqual(top.formals['switch'],{'enable':'read','status':'inout','done':'write'})

    def test_dynamic_and_unproven_named_actuals_do_not_bind(self):
        from plc_trace.graph.bindings import named_arguments
        call='Work(Input:=Unit[2, 1].OK, Out=>Area[idx], Unknown:=Other, Computed:=A OR B)'
        roles={'input':'read','out':'write','computed':'read'}
        self.assertEqual(named_arguments(call,roles),[['read','Unit[2,1].OK','Input']])
        self.assertFalse(named_arguments('Work(Input:=A, Input:=B)',roles))

    def test_contact_to_coil_parallel_paths_and_unknown_block_boundary(self):
        def coil(key, before, name):
            return (f'<coil localId="{key}"><connectionPointIn><connection refLocalId="{before}"/>'
                    f'</connectionPointIn><variable>{name}</variable></coil>')
        ld = (contact(1,[0],'Left')+contact(2,[0],'Right')+contact(3,[1,2],'Permit')+coil(4,3,'Handoff')+
              '<block localId="5" typeName="VendorBlock"><inputVariables>'+port('EN',1)+
              '</inputVariables></block>'+coil(6,5,'Opaque'))
        g = self.model(declarations('Left','Right','Permit','Handoff','Opaque'),program('Cell',ladder(ld)))
        refs = {g['symbols'][dest]['name']:{g['symbols'][s]['name'] for s in inputs}
                for dest,inputs,*_ in g['plant']['dependencies']}
        self.assertEqual(refs, {'Handoff':{'Left','Right','Permit'}})

    def test_readonly_shared_input_and_writable_reference_are_not_cross_writers(self):
        bind = ('<block localId="2" typeName="Lib"><inOutVariables><variable formalParameter="Ref">'
                '<expression>Bus</expression></variable></inOutVariables></block>')
        g = self.model(declarations('Bus','Phase'), program('A',ladder(bind)),
                       program('B',self.st('IF Phase=10 AND Bus THEN Phase:=20; END_IF;')))
        self.assertEqual(g['plant']['signal_buses'], [])
        self.assertTrue(next(s for s in g['symbols'] if s['name']=='Bus')['writes'])


if __name__ == '__main__':
    unittest.main()
