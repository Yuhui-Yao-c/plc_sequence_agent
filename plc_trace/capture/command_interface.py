"""Identify station command records from bound selectors, phase writes and their effects."""
from collections import defaultdict

from plc_trace.capture.coordination_scope import arrival_only
from plc_trace.capture.production_ownership import matches_terminal_read
from plc_trace.capture.station_scope import writer_context
from plc_trace.source.source_expressions import required_atoms, source_condition


def _command_gates(write):
    guard = write.get('operating_guard')
    if guard:
        for atom in required_atoms(source_condition(guard), {'eq'}).values():
            key, value = atom.args
            if key.casefold() in write.get('global_reads', ()):
                yield key.casefold(), value


def _declared(row, path):
    return any(d['kind'] == 'declaration' and d['path'].casefold() == path for d in row['declarations'])


def _phase(site, step):
    cases = tuple(tuple(f) for f in site.get('frames', ()) if f[0] == 'case')
    return writer_context(site), site.get('network'), site.get('source_line'), cases, step


class CommandInterfaces:
    """Concrete command records and the job cursors launched on the same station instance."""

    def __init__(self, rows):
        self.owners = {}
        gates = {}
        witnessed = {r['identity']: r for r in rows if r['sequence_witness'] and r['eligibility'] == 'scope_required'}
        for name, row in witnessed.items():
            record = name.rpartition('.')[0]
            station = record.rpartition('.')[0]
            if not station or not _declared(row, station) or not _declared(row, record):
                continue
            values = defaultdict(set)
            phases = defaultdict(set)
            for write in row['transitions']:
                for key, value in _command_gates(write):
                    if key != name and key.rpartition('.')[0] == record:
                        values[key].add(value)
                        phases[key].add(_phase(write, write['from']))
            selectors = {key for key, seen in values.items() if len(seen) >= 2}
            if not selectors:
                continue
            equipment = {_phase(e, e['step']) for e in row['effects'] if (target := e.get('global_target'))
                         and target.startswith(station + '.') and not target.startswith(record + '.')}
            matched = {(key, value) for key in selectors if phases[key] & equipment for value in values[key]}
            if matched:
                self.owners[name] = station
                gates[name] = matched

        # Resolve the parent first. A cursor under another station's command gate
        # or a private variable on the calling program has a different storage owner.
        self.stations = set(self.owners.values())
        if not self.stations:
            return
        commands = self.owners.copy()
        for name, row in witnessed.items():
            if name in commands:
                continue
            parents = {link['parent'].casefold() for link in row['control_links']
                       if link['child'].casefold() == name and link['parent'].casefold() in commands}
            for parent in sorted(parents, key=lambda key: (-len(commands[key]), key)):
                station = commands[parent]
                if (name.startswith(station + '.') and _declared(row, station)
                        and any(g in gates[parent] for w in row['transitions'] for g in _command_gates(w))
                        and not arrival_only(row, witnessed)):
                    self.owners[name] = station
                    break

    def private_protocol(self, row):
        """Identify the specific station a closed local serves, leaving independent progress visible."""
        activation = row.get('activation')
        if not self.stations or not activation or activation['open_lines'] or row['identity'] in self.owners:
            return None
        waits = [l for l in row['control_links'] if l['kind'] == 'waits_for_child' and l['child'].casefold() == row['identity']]
        if waits:
            # A station which inspects distinct intermediate states can time more
            # than completion. Keep that claim available for the ordinary review.
            values = {l['child_value'] for l in waits}
            reads = [u for u in activation['read_uses'] if u['kind'] != 'dispatch']
            if (len(values) != 1 or not reads or any(l['parent'].casefold() not in self.owners for l in waits)
                    or any(w['from'] in values for w in row['transitions'])
                    or not all(any(matches_terminal_read(u, l) for l in waits) for u in reads)):
                return None
            return {'kind': 'station_terminal_review', 'parents': sorted({l['parent'] for l in waits}),
                    'links': [l['id'] for l in waits], 'terminal': next(iter(values)),
                    'reason': '工位只读取本局部流程的完成值。单独计时还需核实本件作业的独立阶段或结果交接。'}

        writes = row['transitions']
        if not writes or any(not w.get('binding') for w in writes):
            return None
        outputs = {e['global_target'] for e in row['effects'] if e.get('global_target')}
        common = None
        lines = set()
        for write in writes:
            call = write['binding'][-1]
            actuals = {v.casefold() for v in call.get('reference_actuals', {}).values() if isinstance(v, str) and v}
            bound = {port for port in actuals if any(port == s or port.startswith(s + '.') for s in self.stations)
                     and any(t == port or t.startswith(port + '.') for t in outputs)}
            common = bound if common is None else common & bound
            if not common:
                return None
            lines.add(call['source_line'])
        return {'kind': 'station_port_review', 'ports': sorted(common), 'call_lines': sorted(lines),
                'reason': '本局部游标通过功能块实参控制工位设备。已定位设备端口，独立的工件作业或结果交接仍需核实。'}
