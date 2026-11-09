"""Source call bindings used by Trace description evidence."""
from __future__ import annotations

import re
from collections import defaultdict

from boundary_logic import (FALSE, TRUE, IDENT, BoundarySyntaxError, combine,
                            integer, negate, parse, symbol)
from fb_transport import bound_expr

_WRITE = {'coil_set', 'coil_reset', 'coil_write'}
_AXIS = {'axismove', 'mc_moveabsolute', 'mc_moverelative', 'mc_movevelocity'}
_REQUEST = {
    'autoanastole': '原位侧请求', 'hmianastole': '原位侧请求',
    'autostrout': '工位侧请求', 'hmistrout': '工位侧请求',
}
_DEVICE = re.compile(r'(?:_?axis|cy(?:_\d+)?)', re.I)


def named_arguments(text):
    """Read the native ledger's semicolon-separated named actuals."""
    actuals, repeated = {}, set()
    for part in text.split(';'):
        name, sep, actual = part.partition('=')
        name = name.strip().casefold()
        if sep and re.fullmatch(r'[^\W\d]\w*', name):
            if name in actuals:
                repeated.add(name)
            actuals[name] = actual.strip()
    return {name: actual for name, actual in actuals.items() if name not in repeated}


def owner(scope):
    return scope.split('::', 1)[0].casefold()


def _guard(expr, actuals):
    if expr.kind == 'const':
        return expr
    if expr.kind == 'not':
        return negate(_guard(expr.args[0], actuals))
    if expr.kind in ('and', 'or'):
        return combine(expr.kind, *(_guard(term, actuals) for term in expr.args))
    bound = bound_expr(expr.text(), actuals)
    if expr.kind == 'signal' and integer(bound) in (0, 1):
        return TRUE if integer(bound) == 1 else FALSE
    if expr.kind in ('xor', 'opaque_cmp'):
        raise BoundarySyntaxError('Unsupported call guard')
    return parse(bound)


def device_label(text):
    """Keep the Chinese equipment name, including the side and mechanism."""
    text = re.sub(r'^[A-Za-z]+\d+[_\s-]+', '', text.strip())
    text = re.sub(r'_([A-Z])(?=[\u4e00-\u9fff])', r'\1侧', text)
    text = re.sub(r'[_\s]+', '', text)
    return re.sub(r'(真空)(?:吸附|吸)$', r'\1', text)


class CallEvidence:
    def __init__(self):
        self.bodies = defaultdict(list)
        self.labels = defaultdict(list)

    def add(self, row):
        scope = row['scope']
        name, _, body = scope.partition('::')
        if body == 'body' and row['command'].casefold() in _WRITE and row['guard_complete'] == 'True':
            self.bodies[name.casefold()].append(row)
        # A device declaration/configuration call can name one actual object.
        # State-specific rung comments describe operations and are handled separately.
        if row['start'] or not row['comment']:
            return
        devices = [symbol(actual) for formal, actual in named_arguments(row['arguments']).items()
                   if _DEVICE.fullmatch(formal) and re.fullmatch(IDENT, actual)]
        if len(devices) == 1:
            label = device_label(row['comment'])
            if re.fullmatch(r'[\u4e00-\u9fffA-Z0-9（）()]+', label) and re.search(
                    r'(气缸|电机|伺服轴|真空|吸盘|夹爪|机械手|机器人)$', label):
                self.labels[owner(scope), devices[0].casefold()].append({
                    'symbol': devices[0], 'label': label, 'scope': scope,
                    'network': row['network'], 'source_line': row['source_line'],
                    'command': row['command'], 'text': row['comment'],
                })

    def binding(self, scope, actual):
        labels = self.labels.get((owner(scope), actual.casefold()), [])
        return labels[0] if len({entry['label'] for entry in labels}) == 1 else None

    def effects(self, call, actuals):
        """Resolve writes on a named actual without evaluating live conditions."""
        for write in self.bodies.get(call['command'].casefold(), ()):
            target = write['arguments'].strip()
            if not re.fullmatch(IDENT, target):
                continue
            formal = re.split(r'[.\[]', symbol(target), maxsplit=1)[0].casefold()
            actual = actuals.get(formal, '')
            if not re.fullmatch(IDENT, actual):
                continue
            try:
                guard = _guard(parse(write['guard']), actuals)
            except BoundarySyntaxError:
                continue
            if guard == FALSE:
                continue
            yield {
                'formal': formal, 'actual': symbol(actual),
                'write': write['command'].casefold(), 'target': bound_expr(target, actuals),
                'callee_scope': write['scope'], 'callee_line': write['source_line'],
                'callee_node': write['node'], 'original_guard': write['guard'], 'bound_guard': guard.text(),
            }


def action_specs(command, actuals, effects):
    """Describe only controls with a known interface or bound request write."""
    kind = command['command'].casefold()
    if kind in _AXIS:
        actual = actuals.get('_axis') or actuals.get('axis', '')
        if re.fullmatch(IDENT, actual):
            position = integer(actuals.get('_position') or actuals.get('position', ''))
            target = ''
            if position is not None:
                if kind == 'axismove':
                    target = f'至{position}号位'
                else:
                    # Vendor motion blocks may take engineering coordinates.
                    # A literal coordinate does not identify a numbered station.
                    target = f'（位置参数{position}）'
            yield symbol(actual), '轴', '运行' if kind == 'mc_movevelocity' else '定位', target, []
        return
    by_object = defaultdict(list)
    for effect in effects:
        member = effect['target'].removeprefix(effect['actual'] + '.').casefold()
        if effect['write'] == 'coil_set' and member in _REQUEST:
            by_object[effect['actual']].append((_REQUEST[member], effect))
    for actual, requests in by_object.items():
        directions = {request for request, _ in requests}
        if len(directions) == 1:
            yield actual, '', requests[0][0], '', [effect for _, effect in requests]
