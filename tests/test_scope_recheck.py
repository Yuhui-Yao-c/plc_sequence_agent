import unittest

from plc_trace.capture.scope_recheck import scope_recheck


def instance(name, **changes):
    binding = {'scope': 'Line', 'instance': name, 'source_line': 9, 'type': 'Transfer',
               'guard': 'TRUE', 'guard_complete': True, 'reference_actuals': {}}
    write = {'scope': 'Transfer', 'id': 't1', 'kind': 'plcopen_wired_move', 'source_line': 24,
             'guard_complete': True, 'guard': 'TRUE', 'selector': name + '.Cursor',
             'from': 4, 'to': 7, 'binding': [binding], **changes}
    return {'name': name, 'identity': name.casefold(), 'sequence_witness': True, 'transitions': [write],
            'effects': [], 'control_links': []}


def decisions(name, admission):
    return {'name': name, 'admission': admission, 'boundaries': [{'write': 't1'}],
            'timing_basis': {'boundary_write': 't1'}}


class ScopeRecheckTests(unittest.TestCase):
    def compare(self, peer, other, required=()):
        inventory = {'candidates': [peer, other]}
        roles = [decisions(peer['name'], 'captured'), decisions(other['name'], 'optional')]
        return scope_recheck(inventory, roles, required)

    def test_same_instruction_in_two_instances_has_auditable_source_and_peer(self):
        peer, other = instance('East'), instance('West')
        found, = self.compare(peer, other)
        self.assertEqual((found['kind'], found['name'], found['peer'], found['write'], found['source_line']),
                         ('same_bound_operation', 'West', 'East', 't1', 24))
        self.assertEqual(self.compare(peer, other, ['west']), [])

    def test_source_location_member_call_condition_and_target_step_are_significant(self):
        peer = instance('East')
        for change in ({'source_line': 25}, {'selector': 'West.Recipe'}, {'guard_complete': False},
                       {'to': 8}, {'kind': 'st_assign'}, {'binding': []}):
            with self.subTest(change=change):
                self.assertEqual(self.compare(peer, instance('West', **change)), [])
        other = instance('West')
        other['transitions'][0]['binding'][0]['guard'] = 'Maintenance'
        self.assertEqual(self.compare(peer, other), [])

    def test_omitted_owner_of_an_enclosed_child_is_reviewed(self):
        owner, child = instance('Owner'), instance('Unit')
        inventory = {'candidates': [owner, child]}
        roles = [decisions('Owner', 'optional'), decisions('Unit', 'optional')]
        roles[1]['exclusion_evidence'] = {'kind': 'enclosed_child', 'parent': 'Owner', 'start_step': 10,
                                          'wait_step': 30, 'parent_exit_write': 't4'}
        found, = scope_recheck(inventory, roles)
        self.assertEqual((found['kind'], found['name'], found['children'][0]['write']),
                         ('missing_timing_owner', 'Owner', 't4'))
        self.assertEqual(scope_recheck(inventory, roles, ['owner']), [])
