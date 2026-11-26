"""Account for executable instructions throughout an encapsulated cursor's CASE body."""
import re
from collections import defaultdict

from plc_trace.analysis.successor_graph import STParser
from plc_trace.source.plcopen_trace_control import ERRORS
from plc_trace.source.source_expressions import source_condition


def _phase_captions(source, parser):
    """Literal captions duplicate the arm's comment on the first instruction."""
    labels = defaultdict(lambda: defaultdict(set))
    targets = {}
    for name, literal, _, offset, _ in parser.statements:
        if len(literal) < 3 or literal[0] not in ('"', "'") or literal[-1] != literal[0]:
            continue
        frames = parser.frames.get(offset, ())
        if not frames or frames[-1][0] != 'case':
            continue
        _, dispatch, arm, _ = frames[-1]
        note = re.fullmatch(r'[-+\d\s,.]+:\s*//([^\n\r]*)\s*', source[arm:offset])
        if note and note[1].strip() == literal[1:-1]:
            targets[offset] = name.casefold()
            labels[name.casefold()][dispatch].add((arm, literal))
    owners = {name: [case for case, stages in cases.items() if len({s for _, s in stages}) >= 2]
              for name, cases in labels.items()}
    return targets, owners


def dispatch_operations(source, safe_code, parser, selectors, shift):
    from plc_trace.capture.sysmac_discovery import _case_regions

    if not selectors:
        return {}
    regions = _case_regions(safe_code)
    captions, owners = _phase_captions(source, parser)
    audits = {}
    for selector in selectors:
        for start, end in regions.get(selector, ()):
            # Parse the original CASE, including arms without any recognized state write.
            # A loop or opaque call removed by another analysis cannot certify enclosure.
            body = STParser(safe_code[start:end], condition_parser=source_condition, strict=False, retain_source=True)
            try:
                body.seq()
            except ERRORS:
                continue
            offsets = {p for _, _, _, p, _ in body.statements}
            if any(body.spans[s['token']][0] not in offsets for s in body.skipped):
                continue
            # A function embedded in a condition or RHS may write through a reference.
            if any(t == '(' and i
                   and body.ts[i-1].upper() not in {'(', 'IF', 'ELSIF', 'AND', 'OR', 'XOR', 'NOT', '=', '<>', '<', '>', '<=', '>='}
                   for i, t in enumerate(body.ts)):
                continue
            writes, notes = [], []
            for pos in sorted(start + p for p in offsets):
                if pos in captions:
                    notes.append([case + shift for case in owners[captions[pos]] if case != start])
                else:
                    writes.append(pos + shift)
            audits[start] = {'writes': writes, 'caption_cases': notes}
    return audits
