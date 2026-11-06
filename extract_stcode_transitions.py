# -*- coding: utf-8 -*-
"""v9.27 STCode state-comparison supplement for PLCopen projects.

The LD/FBD structural analyser (plcopen_controlflow.py) finds step-variable
state comparisons by tracing MOVE+EQ block wiring.  But some state comparisons
exist only as text inside <STCode> elements — e.g.:

    gaxActionTrig[3]:=(udt_EM01.adiAutoStep[1]=110);   // 顶盖焊
    IF udt_EM12_2.adiAutoStep[2]=310 THEN ...           // 顶盖焊

These are invisible to LD structure analysis.  This module supplements the
candidate pool by scanning <STCode> text for  var[idx]=<int>  comparisons.

Filtering — NOT a hardcoded whitelist:
  The caller passes *ld_var_names*, the set of array-variable names that the
  LD structural analyser already discovered (adiAutoStep, Process_steps,
  AutoStep, iCmdStep, m_nNextAutoStep, …).  Only STCode matches whose
  variable name is in this set are kept.  This makes LD the "variable-name
  discoverer" and STCode the "same-name supplement" — no enumeration, no
  project-specific hardcoding.

  Matches like  Location_Speed[2]=0  or  PcToPlc_INT[5]=1  (communication /
  speed / camera data) are filtered out because their variable names never
  appear as state controllers in LD.

Assignment exclusion:
  The regex uses (?<!:) so that  var[idx]:=value  (assignment) is NOT
  matched — only  var[idx]=value  (comparison) is.

Output:
  Atomic candidates only — each match becomes one row with
  开始标签 = 结束标签 = "var[idx]=value", 状态 = "stcode_state_compare".
  No migration pairs are composed (no start→end guessing); the downstream
  pipeline and LLM batches handle process semantics.

Usage (called by agent.py PLCopen path):
  from extract_stcode_transitions import generate
  result = generate(source, out, ld_var_names)
  # result = {'candidates': [...rows...], 'match_count': N, 'kept_count': M, ...}
"""
import csv, json, re
from pathlib import Path

try:
    from lxml import etree
except ImportError:
    etree = None


# Match  var[idx]=<int>  but NOT  var[idx]:=value  (assignment).
# The (?<!:) lookbehind ensures the = is not preceded by ':'.
# Captures: 1 = full symbol (instance.array_var[idx]), 2 = value
_STCODE_CMP_RE = re.compile(
    r'(?<!:)'
    r'([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\[\d+\])'   # var[idx]
    r'\s*=\s*'
    r'(\d+)'                                       # compared value
)


def _local(e):
    if etree is None:
        return ''
    return etree.QName(e).localname if isinstance(e.tag, str) else ''


def _iter_stcode_text(source: str):
    """Yield text of each <STCode> element in the PLCopen XML."""
    if etree is None:
        return
    for _, e in etree.iterparse(source, events=('end',), tag='{*}STCode',
                                huge_tree=True, resolve_entities=False,
                                no_network=True):
        t = e.text or ''
        if t:
            yield t
        e.clear()
        while e.getprevious() is not None:
            p = e.getparent()
            if p is not None:
                del p[p.index(e) - 1]
            else:
                break


def _var_name_of(symbol: str) -> str:
    """Extract the array-variable name from a symbol like udt_EM01.adiAutoStep[1].

    Returns 'adiAutoStep' (the part after the last dot, before [).
    For a bare  Process_steps[0]  (no dot) returns 'Process_steps'.
    """
    base = symbol.rsplit('.', 1)[-1]   # adiAutoStep[1]
    return base.split('[', 1)[0]       # adiAutoStep


def _owner_pou(e):
    """Walk up to find the enclosing <pou> or <action> name for evidence."""
    p = e
    while p is not None:
        ln = _local(p)
        if ln in ('pou', 'action'):
            return p.get('name') or ''
        p = p.getparent() if hasattr(p, 'getparent') else None
    return ''


def generate(source: str, out, ld_var_names: set) -> dict:
    """Extract STCode state comparisons, filtered by ld_var_names.

    Parameters
    ----------
    source : str
        PLC source XML path.
    out : str or Path
        Run output directory (for writing audit CSV; candidate rows are
        returned to the caller for pool append).
    ld_var_names : set of str
        Array-variable names discovered by LD structural analysis
        (e.g. {'adiAutoStep', 'Process_steps', ...}).  Only STCode matches
        whose variable name is in this set are kept.

    Returns
    -------
    dict with:
      'candidates': list of candidate-row dicts (FIELDS-compatible)
      'match_count': total raw matches before filtering
      'kept_count': matches after ld_var_names filtering
      'filtered_count': matches filtered out (non-step-variable)
      'symbols': set of unique symbols kept (for audit)
    """
    out = Path(out)
    ld_var_names = set(ld_var_names or [])

    candidates = []
    total_matches = 0
    kept_matches = 0
    filtered_matches = 0
    kept_symbols = set()
    filtered_samples = []

    for st_text in _iter_stcode_text(source):
        for m in _STCODE_CMP_RE.finditer(st_text):
            total_matches += 1
            symbol = m.group(1)        # udt_EM01.adiAutoStep[1]
            value = m.group(2)         # 110
            var_name = _var_name_of(symbol)   # adiAutoStep

            if var_name not in ld_var_names:
                filtered_matches += 1
                if len(filtered_samples) < 10:
                    filtered_samples.append(f'{symbol}={value} ({var_name})')
                continue

            kept_matches += 1
            kept_symbols.add(symbol)
            label = f'{symbol}={value}'
            # Atomic candidate: start == end == label.
            # No migration pair composition; downstream handles semantics.
            candidates.append({
                '构成要素': symbol,
                '动作': 'STCode状态比较',
                '动作描述': '',
                '开始标签': label,
                '结束标签': label,
                '状态': 'stcode_state_compare',
                '证据POU': '',
                '证据节点': '',
                '原始条件': '',
                '控制器': symbol,
                '分支数': 0,
            })

    # Write audit CSV for transparency.
    audit_path = out / 'stcode_state_compare.csv'
    fields = ['构成要素', '动作', '动作描述', '开始标签', '结束标签',
              '状态', '证据POU', '证据节点', '原始条件', '控制器', '分支数']
    with audit_path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(candidates)

    # Write audit JSON summary.
    summary_path = out / 'stcode_state_compare_summary.json'
    summary = {
        'total_raw_matches': total_matches,
        'kept': kept_matches,
        'filtered_out': filtered_matches,
        'ld_var_names_count': len(ld_var_names),
        'ld_var_names': sorted(ld_var_names),
        'unique_symbols_kept': len(kept_symbols),
        'filtered_samples': filtered_samples,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding='utf-8')

    return {
        'candidates': candidates,
        'match_count': total_matches,
        'kept_count': kept_matches,
        'filtered_count': filtered_matches,
        'symbols': kept_symbols,
        'summary': summary,
    }
