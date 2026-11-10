"""Atomic source labels and stable identifiers used in Trace evidence."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from boundary_logic import BoundarySyntaxError, Expr, parse


PROVEN = {'plcopen_wired_guard','st_structured_guard','smc2_grid_guard',
          'smc2_embedded_st_guard','omron_native_wired_guard','omron_native_st_guard'}

def stable_id(prefix: str, value: Any) -> str:
    b=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    return prefix+hashlib.sha256(b).hexdigest()[:16]

def atomic(text: str) -> Expr | None:
    try:
        e=parse(text)
        return e if e.kind in ('eq','signal') else None
    except (BoundarySyntaxError, TypeError):
        return None
