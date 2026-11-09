"""Source-only scalar predicates. NOT a GT repair or whole ST interpreter.

Scalar relational tests outside the small boundary grammar are represented as
explicit symbolic predicates with a source-text sidecar. They are never actual
PLC tag names and never qualify a NOT -> successor equivalence proof.
"""
from __future__ import annotations
import hashlib
import re
from decimal import Decimal, InvalidOperation
from operator import add, mul, sub

from boundary_logic import (parse, Expr, BoundarySyntaxError, IDENT, combine,
                            integer, negate, symbol)

SCALAR = re.compile(rf'(?:{IDENT}|(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?|[+*/()\-])', re.I)


def scalar_reads(text: str) -> list[str]:
    return sorted(set(re.sub(r'\s+', '', m.group()) for m in re.finditer(IDENT, text)
                      if m.group().upper() not in {'INT', 'DINT', 'REAL', 'TRUE', 'FALSE'}))


def valid_scalar(text: str) -> bool:
    """No calls, indexing expressions with side effects, semicolons or flow."""
    if re.search(r'\b(?:IF|THEN|ELSE|RETURN|FOR|WHILE|CASE)\b|:=|;',text,re.I):return False
    if re.search(rf'(?:{IDENT})\s*\(', text):return False
    pos=0;depth=0;expect_operand=True
    for m in SCALAR.finditer(text):
        if text[pos:m.start()].strip():return False
        t=m.group();pos=m.end()
        if t=='(':
            if not expect_operand:return False
            depth+=1
        elif t==')':
            if expect_operand or not depth:return False
            depth-=1
        elif t in '+-*/':
            if expect_operand and t not in '+-':return False
            expect_operand=True
        else:
            if not expect_operand:return False
            expect_operand=False
    return not text[pos:].strip() and not depth and not expect_operand


class SourceConditions:
    def __init__(self):self.definitions={}

    def relational(self, left: str, op: str, right: str) -> Expr:
        if op not in ('<','>','<=','>=','=','<>','!=','==') or not valid_scalar(left) or not valid_scalar(right):
            raise BoundarySyntaxError('unsupported source scalar predicate')
        text=f'{left.strip()} {op} {right.strip()}'
        key='__source_predicate_'+hashlib.sha256(text.casefold().encode()).hexdigest()[:16]
        self.definitions[key]={'id':key,'kind':'scalar_relation','expression':text,
            'reads':scalar_reads(left)+scalar_reads(right),'not_a_plc_tag':True,
            'proof':'symbolic_source_predicate_not_numeric_or_temporal_proof'}
        return Expr('signal',(key,))

    def parse(self, text: str) -> Expr:
        try:return parse(text)
        except BoundarySyntaxError:
            # This extension deliberately supports only a single scalar test.
            # Unknown Boolean mixtures must not be repaired or partially parsed.
            if re.search(r'\b(?:AND|OR|NOT|XOR)\b',text,re.I):raise
            m=re.fullmatch(r'\s*(.+?)\s*(>=|<=|<>|!=|==|>|<|=)\s*(.+?)\s*',text,re.S)
            if not m:raise
            return self.relational(*m.groups())

    def expand(self,text: str) -> str:
        for key,d in self.definitions.items():text=text.replace(key,'('+d['expression']+')')
        return text


# The Trace source view can retain a partially understood guard. These helpers
# leave the boundary parser and its proof grammar intact.
DECIMAL = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?'
_REAL = rf'(?:(?:REAL|LREAL)\s*#\s*)?{DECIMAL}'
_RELATIONS = {'=': 'veq', '==': 'veq', '<>': 'vne', '!=': 'vne',
              '<': 'lt', '<=': 'le', '>': 'gt', '>=': 'ge'}
_REVERSE = {'lt': 'gt', 'le': 'ge', 'gt': 'lt', 'ge': 'le'}
_RELATION = re.compile(rf'({_REAL}|{IDENT})\s*([<>=!]=?|<>)\s*({_REAL}|{IDENT})', re.I)
_GUARD_TOKEN = re.compile(rf'''{IDENT}|"(?:""|[^"])*"|'(?:''|[^'])*'|[^\s]''')
_ARITHMETIC = re.compile(
    rf'\s*(?:(?P<literal>(?:2|8|16)\s*#\s*[0-9A-Fa-f]+|'
    rf'[A-Za-z]+\s*#\s*[+-]?\d+|\d+)|(?P<name>{IDENT})|(?P<op>[()+*\-]))')
INTEGER_OPERATORS = {'+': (1, add), '-': (1, sub), '*': (2, mul)}
INTEGER_BOUNDS = {
    prefix + base: (0, 2**bits - 1) if prefix else (-2**(bits - 1), 2**(bits - 1) - 1)
    for base, bits in (('SINT', 8), ('INT', 16), ('DINT', 32), ('LINT', 64))
    for prefix in ('', 'U')
}


def combine_wiring(kind, *parts):
    """Factor shared prefixes across parallel branches before checking size."""
    result = combine(kind, *parts)
    if result.kind != 'or':
        return result
    arms = [{x.key(): x for x in (arm.args if arm.kind == 'and' else (arm,))}
            for arm in result.args]
    shared = set(arms[0]).intersection(*arms[1:])
    if not shared:
        return result
    remainder = [combine('and', *(x for key, x in arm.items() if key not in shared))
                 for arm in arms]
    return combine('and', *(arms[0][key] for key in shared), combine_wiring('or', *remainder))


def source_condition(raw):
    """Preserve Boolean topology and real comparisons around opaque operands."""
    raw = raw.strip()
    try:
        return parse(raw)
    except ValueError:
        pass
    levels = {operator: [] for operator in ('OR', 'XOR', 'AND')}
    depth, closing = 0, -1
    tokens = list(_GUARD_TOKEN.finditer(raw))
    for token in tokens:
        word = token[0].upper()
        if word == '(':
            depth += 1
        elif word == ')':
            depth -= 1
            if depth < 0:
                break
            if depth == 0 and closing < 0:
                closing = token.end()
        elif depth == 0 and word in levels:
            levels[word].append(token)
    if tokens and depth == 0:
        for operator, cuts in levels.items():
            if not cuts:
                continue
            left, parts = 0, []
            for cut in cuts:
                parts.append(raw[left:cut.start()].strip())
                left = cut.end()
            parts.append(raw[left:].strip())
            if all(parts):
                children = tuple(source_condition(part) for part in parts)
                return Expr('xor', children) if operator == 'XOR' else combine_wiring(operator.lower(), *children)
        if raw.startswith('(') and closing == len(raw):
            return source_condition(raw[1:-1])
        if len(tokens) > 1 and tokens[0][0].upper() == 'NOT':
            return negate(source_condition(raw[tokens[0].end():]))
        if match := _RELATION.fullmatch(raw):
            left, operator, right = match.groups()
            kind = _RELATIONS.get(operator)
            if kind:
                if re.fullmatch(_REAL, left, re.I):
                    left, right, kind = right, left, _REVERSE.get(kind, kind)
                if re.fullmatch(IDENT, left) and re.fullmatch(_REAL, right, re.I):
                    try:
                        value = Decimal(right.rsplit('#', 1)[-1].strip())
                    except InvalidOperation:
                        pass
                    else:
                        return Expr(kind, (symbol(left), str(value)))
    return Expr('opaque_cmp', (f'@condition({raw})',))


def required_atoms(guard, kinds):
    """Return atoms required by every branch, without expanding the guard."""
    if guard.kind in kinds:
        return {guard.key(): guard}
    if guard.kind == 'and':
        return {key: atom for child in guard.args for key, atom in required_atoms(child, kinds).items()}
    if guard.kind == 'or' and guard.args:
        arms = [required_atoms(child, kinds) for child in guard.args]
        shared = set(arms[0]).intersection(*arms[1:])
        return {key: arms[0][key] for key in sorted(shared)}
    return {}


def state_write_value(raw, variable, value, bounds):
    """Evaluate constant and self-relative integer assignments within their type."""
    raw = raw.strip()
    try:
        literal = integer(raw)
    except ValueError:
        return None
    if literal is not None:
        prefix = raw.split('#', 1)[0].strip().upper()
        checks = (limits for limits in (bounds, INTEGER_BOUNDS.get(prefix)) if limits)
        return literal if all(low <= literal <= high for low, high in checks) else None
    if bounds is None or not 0 < len(raw) <= 512:
        return None
    tokens, cursor = [], 0
    while cursor < len(raw):
        match = _ARITHMETIC.match(raw, cursor)
        if match is None:
            return None
        tokens.append((match.lastgroup, match[match.lastgroup]))
        cursor = match.end()

    # Keep all intermediate results in the declared domain and in the
    # explicitly typed literal domains. An overload or wrap is left open.
    limits = [bounds]
    limits.extend(INTEGER_BOUNDS[prefix] for kind, item in tokens if kind == 'literal'
                  and (prefix := item.split('#', 1)[0].strip().upper()) in INTEGER_BOUNDS)
    low = max(pair[0] for pair in limits)
    high = min(pair[1] for pair in limits)
    pos = 0

    def checked(number):
        if number is None or not low <= number <= high:
            raise ValueError('integer range')
        return number

    def expression(minimum=0):
        nonlocal pos
        kind, item = tokens[pos]
        pos += 1
        if item == '(':
            result = expression()
            if tokens[pos][1] != ')':
                raise ValueError('missing closing parenthesis')
            pos += 1
        elif item in ('+', '-'):
            result = checked((1 if item == '+' else -1) * expression(3))
        elif kind == 'literal':
            result = checked(state_write_value(item, variable, value, bounds))
        elif kind == 'name' and symbol(item).casefold() == variable:
            result = checked(value)
        else:
            raise ValueError('unknown arithmetic operand')
        while pos < len(tokens) and tokens[pos][1] in INTEGER_OPERATORS:
            precedence, apply = INTEGER_OPERATORS[tokens[pos][1]]
            if precedence < minimum:
                break
            pos += 1
            result = checked(apply(result, expression(precedence + 1)))
        return result

    try:
        result = expression()
        return result if pos == len(tokens) else None
    except (IndexError, ValueError, RecursionError):
        return None
