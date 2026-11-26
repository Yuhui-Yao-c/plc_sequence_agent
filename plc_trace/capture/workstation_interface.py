"""Identify declared production charts and the equipment driven alongside them."""
import re
from collections import defaultdict

from plc_trace.analysis.boundary_logic import IDENT, compatible, substitute, symbol
from plc_trace.capture.action_obligations import action_obligation
from plc_trace.capture.capture_use import _guard_signals
from plc_trace.capture.command_interface import CommandInterfaces
from plc_trace.capture.coverage import array_storage
from plc_trace.capture.station_scope import writer_context
from plc_trace.capture.step_discovery import fixed_path
from plc_trace.source.source_expressions import source_condition


# These are affirmative descriptions on the declared storage itself. An address,
# routine name or a comment on an enclosing structure cannot establish its purpose.
_CHART = re.compile(
    r'(?:\d+个)?(?:工位)?(?:自动(?:程序步|(?:流程)?步(?:序|骤|程序|状态)?)|工艺步骤)|'
    r'(?:(?:station|workstation)\s+)?(?:auto(?:matic)?|production|process)\s+'
    r'(?:(?:sequence|program|cycle)\s+)?(?:steps?|phases?)(?:\s+(?:array|index))?', re.I)
_ACTUATOR = re.compile(
    r'[\u3400-\u9fff]*(?:气缸|油缸|滚筒|电机|马达|伺服轴|电磁阀)|'
    r'(?:(?:pneumatic|hydraulic|stop|drive|servo|conveyor)\s+)*(?:cylinder|roller|motor|valve|axis|actuator)',
    re.I)


_SERVICE_SECTION = re.compile(
    r'(?:^|[_\s])(?:manual|maintenance|calibration|alarms?|diagnostics?|手动|标定|报警|维护|诊断)(?:$|[_\s])', re.I)
_INITIAL_SECTION = re.compile(r'(?:^|[_\s])(?:init(?:ial(?:iz(?:e|ation))?)?|reset|初始化|复位)(?:$|[_\s])', re.I)
_RUN_CONTROL = re.compile(
    r'(?:自动|手动)(?:模式)?(?:执行|运行|使能|启动|自动)|(?:自动|手动)模式|'
    r'(?:automatic|auto|manual)\s+(?:(?:run|cycle)\s+)?(?:mode|enable|start|run)', re.I)


def _operands(guard):
    return {symbol(word).casefold() for word in re.findall(IDENT, guard)}


def _assertion(site):
    value = site.get('value')
    if value is None:
        value = {'coil_set': 'TRUE', 'coil_reset': 'FALSE'}.get(site.get('kind'))
    return symbol(site['target']).casefold(), value


def _call_scope(site):
    calls = site.get('binding')
    return calls[0]['scope'] if calls else site['scope']


def _service_section(scope):
    return _SERVICE_SECTION.search(scope.rsplit('::', 1)[-1])


def _writes(row):
    return row['transitions'] or [pair['write'] for pair in row.get('dispatch_writes', ())]


def _declared_chart(row, fields):
    if not any(d['kind'] == 'declaration' and d['path'].casefold() == row['identity']
               and _CHART.fullmatch(d.get('text', '').strip()) for d in row['declarations']):
        return False
    # A description also used for a test sequence or a device's private state
    # cannot, on its own, identify the enclosing station's production interface.
    writes = _writes(row)
    if all(_service_section(_call_scope(w)) for w in writes):
        return False
    for write in writes:
        calls = write.get('binding')
        if calls:
            host = write['program'].rsplit('::', 1)[-1]
            instance = '.'.join([host, *(c['instance'] for c in calls)]).casefold() + '.'
            if row['identity'].startswith(instance):
                member = row['identity'][len(instance):].split('.', 1)[0].split('[', 1)[0]
                field = fields.get((calls[-1]['type'].casefold(), member))
                if not field or field[0] in ('localvars', 'vars'):
                    return False
    return True


def _operating_start(row, candidate, scan):
    """A declared chart slot can merely latch when operation is enabled."""
    if (row['eligibility'] != 'single_write' or len(row['transitions']) != 1
            or row.get('entry_writes') or any(d['kind'] == 'element' and d.get('text') for d in row['declarations'])):
        return None
    write, = row['transitions']
    if (write.get('binding') or write.get('comment') or candidate.dispatches
            or any(e['step'] == write['to'] or not _INITIAL_SECTION.search(_call_scope(e).rsplit('::', 1)[-1])
                   for e in candidate.effects.values())):
        return None
    unit = scan.programs.get(write['program'].rsplit('::', 1)[-1].casefold())
    if not unit:
        return None
    selector = write.get('selector') or row['identity']
    guard = substitute(source_condition(write['guard']), selector, write['from'])
    controls = {}
    for signal in sorted(_guard_signals(guard.text())):
        if not fixed_path(signal):
            return None
        address = scan.sysmac_channel(signal, unit)[0]
        notes = [d for d in scan.channel_declarations(address) if d['path'].casefold() == address.casefold()]
        if not notes or not _RUN_CONTROL.fullmatch(notes[-1].get('text', '').strip()):
            return None
        controls[signal] = notes[-1]['line']
    # A complete alternative must need only declared mode bits. An opaque
    # sensor test in series with the enable cannot establish this finding.
    branches = guard.args if guard.kind == 'or' else (guard,)
    for branch in branches:
        terms = branch.args if branch.kind == 'and' else (branch,)
        atoms = [t.args[0] if t.kind == 'not' else t for t in terms]
        if (all(a.kind == 'signal' and a.args[0].casefold() in controls for a in atoms)
                and compatible(branch, selector, write['from'], check_remaining=True)):
            return {'kind': 'operating_enable_only', 'write': write['id'], 'source_line': write['source_line'],
                    'from': write['from'], 'to': write['to'], 'control_declarations': controls,
                    'reason': f"运行使能可将本地址从 {write['from']} 写至 {write['to']}。"
                              '当前源码证据未显示按该步值执行工件动作或为下一件重新进入该步。'}
    return None


class WorkstationInterfaces:
    def __init__(self, rows, scan, candidates=None):
        self.fields = scan.fb_fields
        declared = [r for r in rows if r['eligibility'] in ('scope_required', 'single_write', 'distributed_write')
                    and _declared_chart(r, self.fields)]
        self.operating_starts = {}
        if candidates is not None:
            for row in declared:
                if proof := _operating_start(row, candidates[row['identity']], scan):
                    self.operating_starts[row['identity']] = proof
        witnessed_arrays = {array_storage(r) for r in declared if r['sequence_witness']}
        witnessed_arrays.discard(None)
        self.channels = {r['identity'] for r in declared if r['sequence_witness'] or r.get('dispatch_writes')
                         or r['transitions'] and array_storage(r) in witnessed_arrays}
        self.commands = CommandInterfaces(rows)
        self.channels.update(self.commands.owners)
        self.channels.difference_update(self.operating_starts)
        self.by_caller = defaultdict(dict)
        self.chart_scopes = defaultdict(set)
        self.chart_outputs = defaultdict(set)
        self.peers_read = defaultdict(set)
        self.qualified_names = set()
        self.qualified = set()
        if not self.channels:
            return
        for row in rows:
            if row['identity'] not in self.channels:
                continue
            station = self.commands.owners.get(row['identity'], row['identity'].rpartition('.')[0])
            hosts = defaultdict(set)
            for site in _writes(row):
                caller = _call_scope(site)
                self.by_caller[caller][row['name']] = station
                host = site['program'].rsplit('::', 1)[-1].casefold()
                hosts[caller].add(host)
                self.chart_scopes[host].add(caller)
                self.peers_read[host].update(_operands(site['guard']))
            for site in row['effects']:
                caller = _call_scope(site)
                if not site.get('binding'):
                    # Native coil effects may carry the scope without the program.
                    for host in hosts[caller]:
                        self.chart_outputs[host].add(_assertion(site))
        # Qualified uses in executable source can expose an FB's own progress to
        # another routine. A normalized selector synthesized by the binder cannot.
        self.qualified = {(name.casefold(), unit[1].casefold() if unit else '') for name, unit in scan.references if '.' in name}
        self.qualified.update((name, owner) for owner, names in scan.wired_operands.items()
                              for name in names if '.' in name)
        self.qualified_names = {name for name, _ in self.qualified}

    def preview(self, rows):
        # A mixed project can also contain a separately implemented production
        # operation. Keep the same source-backed stages used by the omission review.
        return self.channels | {r['identity'] for r in rows if r['eligibility'] == 'scope_required'
                                and r['identity'] not in self.channels and action_obligation(r)
                                and not self.equipment_cycle(r) and not self.separate_service_section(r)}

    def separate_service_section(self, row):
        """Flag a claimed production interval contradicted by the station's execution layout."""
        if not self.channels or row['identity'] in self.channels or row['control_links'] or not row['transitions']:
            return None
        host, _, leaf = row['identity'].partition('.')
        if not self.chart_scopes.get(host):
            return None
        scopes = set()
        for write in row['transitions']:
            scope = write['scope']
            if (write.get('binding') or write.get('program', '').rsplit('::', 1)[-1].casefold() != host
                    or scope in self.chart_scopes[host] or not _service_section(scope)):
                return None
            scopes.add(scope)
        # A visible use in the station chart warrants the normal production review.
        # A coil asserted by both routines cannot identify which of them reached this stage.
        assertions = {_assertion(e) for e in row['effects']}
        outputs = {target for target, value in assertions if value is None or
                   (target, value) not in self.chart_outputs[host]}
        if {leaf, row['identity']}.union(outputs) & self.peers_read[host]:
            return None
        return {'kind': 'service_section_review', 'scopes': sorted(scopes),
                'writes': [w['id'] for w in row['transitions']],
                'reason': '本地游标的已定位写步均在单列的手动、报警或维护例程。工位自动步在其他例程推进，尚缺连接正常生产作业的计时依据。'}

    def equipment_cycle(self, row):
        """Prove a local equipment cycle in a concrete station's calling routine."""
        if (not self.by_caller or row['identity'] in self.channels or row['control_links'] or row.get('other_outputs')
                or not row['effects'] or not row['transitions']):
            return None
        first = row['transitions'][0]
        chain = first.get('binding', ())
        if len(chain) != 1:
            return None
        context = writer_context(first)[1]
        if any(writer_context(s)[1] != context for s in (*row['transitions'], *row['effects'])):
            return None
        call, = chain
        owner, instance = call['type'].casefold(), call['instance'].casefold()
        host = first['program'].rsplit('::', 1)[-1].casefold()
        leaf = row['identity'].rsplit('.', 1)[-1]
        field = self.fields.get((owner, leaf))
        if (row['identity'] != f'{host}.{instance}.{leaf}' or not field or field[0] not in ('vars', 'localvars')
                or row['identity'] in self.qualified_names or (f'{instance}.{leaf}', host) in self.qualified):
            return None
        ports = {formal: actual.casefold() for formal, actual in call.get('reference_actuals', {}).items()
                 if isinstance(actual, str) and actual}
        peers = [name for name, station in self.by_caller.get(call['scope'], {}).items() if station in ports.values()]
        if not peers or call.get('selector_ports'):
            return None
        # A station can consume a published result without ever reading the private phase.
        if any(v.startswith(instance + '.') for v in self.peers_read[host]) or any(
                v.startswith(f'{host}.{instance}.') for refs in self.peers_read.values() for v in refs):
            return None
        reads = set().union(*(_operands(w['guard']) for w in row['transitions'] if w.get('guard_complete')))
        devices = set()
        for effect in row['effects']:
            target = symbol(effect['target']).casefold()
            if str(effect.get('value')).upper() not in ('TRUE', 'FALSE'):
                return None
            root, _, member = target.partition('.')
            if root == instance:
                local = self.fields.get((owner, member))
                if not local or local[0] not in ('vars', 'localvars'):
                    return None
                continue
            matches = [(k, v) for k, v in ports.items() if target.startswith(v + '.')]
            if len(matches) != 1:
                return None
            formal, actual = matches[0]
            port = self.fields.get((owner, formal.casefold()))
            if not port or port[0] != 'inoutvars':
                return None
            note = re.sub(r'[\s_#-]*\d+\s*$', '', port[1]['text']).strip()
            if not _ACTUATOR.fullmatch(note) or not any(v != target and v.startswith(actual + '.') for v in reads):
                return None
            devices.add((port[1]['line'], port[1]['text']))
        if devices:
            return {'kind': 'station_equipment_cycle', 'station_channels': sorted(peers),
                    'call_lines': [call['source_line']],
                    'actuator_ports': [dict(line=n, text=t) for n, t in sorted(devices)],
                    'reason': '调用方推进该工位的自动步。本块的局部步读取执行器反馈，受控写出限于已声明的执行器端口及块内变量。'}
