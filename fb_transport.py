"""PLC function-block parameter bindings and required guard signals."""
from __future__ import annotations

import re
from boundary_logic import Expr, FALSE, BoundarySyntaxError, IDENT, symbol
from successor_graph import WireError, pins, tag, text


def required_signals(x: Expr) -> set[tuple[str,bool]]:
    """Sufficient syntactic implication, AND union / OR intersection, not co-occurrence."""
    if x.kind=='signal':return {(x.args[0].casefold(),True)}
    if x.kind=='not' and x.args[0].kind=='signal':return {(x.args[0].args[0].casefold(),False)}
    if x.kind=='and':return set().union(*(required_signals(a) for a in x.args))
    if x.kind=='or':
        sets=[required_signals(a) for a in x.args if a!=FALSE]
        return set.intersection(*sets) if sets else set()
    return set()


def interface(pou):
    out={}
    for section in pou:
        if tag(section)!='interface':continue
        for group in section:
            for v in group:
                if tag(v)!='variable':continue
                typ=next((n for c in v if tag(c)=='type' for n in c),None)
                out[v.get('name','').casefold()]={
                    'name':v.get('name',''),'direction':tag(group),
                    'bool':typ is not None and tag(typ).upper()=='BOOL',
                    'documentation':' '.join(text(c) for c in v if tag(c)=='documentation')}
    return out


def bindings(node,wire):
    """Named parameters stay instance-local; repeated/ambiguous bindings fail closed."""
    vals={};bad=set()
    for section in ('inputVariables','inOutVariables','outputVariables'):
        for p in pins(node,section):
            name=p.get('formalParameter','').casefold()
            if not name or name in ('en','eno'):continue
            try:value=wire.value(p)
            except (WireError,BoundarySyntaxError):continue
            if not value:continue
            value=symbol(value)
            if name in vals and vals[name]!=value:bad.add(name)
            else:vals[name]=value
    for name in bad:vals.pop(name,None)
    return vals,bad


def bound_expr(code,mapping):
    # Match a formal symbol/root without changing unrelated longer names.
    rx=re.compile(IDENT,re.U)
    def sub(m):
        v=symbol(m.group());root=re.split(r'[.\[]',v,maxsplit=1)[0]
        dest=mapping.get(root.casefold())
        return dest+v[len(root):] if dest else v
    return rx.sub(sub,code)
