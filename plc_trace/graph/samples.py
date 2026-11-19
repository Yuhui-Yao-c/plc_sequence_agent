"""Contemporaneous reads. No interpolation, scan-order inference or inter-record joins."""
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from math import isfinite

from plc_trace.trace.trace_to_pairs import INTEGER_TYPES


class Snapshots:
    def __init__(self, pairs):
        self.pairs = pairs
        self.values = {}
        self.limits = Counter()

    def native(self, recording, variables):
        channels = {k.casefold(): v for k,v in variables.items()}
        for cursor, row in channels.items():
            wanted = self.pairs.get(cursor)
            if not wanted:
                continue
            for index, a, b, stamp in row['jumps']:
                peers = wanted.get((a,b), set()) & channels.keys()
                if not peers:
                    continue
                if bisect_right(row['timestamps'],stamp)-bisect_left(row['timestamps'],stamp) != 1:
                    self.limits['duplicate_cursor_timestamp'] += 1
                    continue
                readings = {}
                for name in sorted(peers):
                    v = channels[name]
                    left = bisect_left(v['timestamps'],stamp)
                    if bisect_right(v['timestamps'],stamp)-left == 1 and v['values'][left] is not None:
                        readings[name] = v['values'][left]
                    else:
                        self.limits['no_unique_simultaneous_operand_sample'] += 1
                if readings:
                    self.values[(cursor,recording,index)] = readings

    def sysmac(self, recording):
        def part(header, channels):
            available = defaultdict(list)
            for name, typ, col in header:
                if typ in INTEGER_TYPES | {'BOOL','REAL','LREAL'}:
                    available[name.casefold()].append((typ,col))
            getters = {n:v[0] for n,v in available.items() if len(v) == 1}
            active = []
            for name, channel in channels.items():
                cursor = name.casefold()
                wanted = {pair:sorted(reads & getters.keys()) for pair,reads in self.pairs.get(cursor,{}).items()}
                if any(wanted.values()):
                    active.append((cursor,channel,wanted))
            if not active:
                return None
            def capture(index, row):
                for name, channel, wanted in active:
                    if not channel.jumps or channel.jumps[-1][0] != index:
                        continue
                    _,a,b,_ = channel.jumps[-1]
                    readings = {}
                    for operand in wanted.get((a,b), ()):
                        typ,col = getters[operand]
                        cell = row[col].strip() if col < len(row) else ''
                        if not cell:
                            continue
                        try:
                            if typ == 'BOOL':
                                value = {'true':1,'false':0,'1':1,'0':0}[cell.casefold()]
                            elif typ in INTEGER_TYPES:
                                value = int(cell)
                            else:
                                value = float(cell)
                            if isfinite(value):
                                readings[operand] = value
                        except (KeyError, ValueError):
                            self.limits['invalid_operand_sample'] += 1
                    if readings:
                        self.values[(name,recording,index)] = readings
            return capture
        return part
