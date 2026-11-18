"""Sampling continuity and state dwells shared by the two Trace readers."""
from collections import Counter
from statistics import median

GAP_FACTOR = 10
GAP_FILE = 'trace_sampling_gaps.json'
CAPTURE_HEAD = '采集首段'
GAP_HEAD = '缺口后首段'


def _regular_period(deltas):
    positive = [d for d in deltas if d > 0]
    if len(positive) < 5:
        return None
    period = median(positive)
    regular = sum(period / 2 <= d <= period * 2 for d in positive)
    return period if 4 * regular >= 3 * len(positive) else None


def clock_breaks(deltas, specified=None):
    """Return the cadence and indices *after* an unusually long sample interval.

    A sustained, locally regular slower cadence has priority over a faster
    recording-wide median. A declared periodic clock can also cover short runs.
    """
    period = specified or _regular_period(deltas)
    cuts = set()
    if period is not None:
        for j, delta in enumerate(deltas):
            if delta <= GAP_FACTOR * period:
                continue
            if specified is None:
                # A regular window immediately beside or containing this interval
                # can establish a local cadence, including at a rate change.
                starts = range(max(0, j - 5), min(j + 1, len(deltas) - 5) + 1)
                if any(delta <= GAP_FACTOR * (_regular_period(deltas[k:k + 5]) or 0) for k in starts):
                    continue
            cuts.add(j + 1)
    return period, cuts


class ChannelSamples:
    """Accumulate observed transitions, including those with a censored start."""

    def __init__(self):
        self.jumps = []
        self.segments = []
        self.dwell_starts = {}
        self.partial_dwells = {}
        self.estimated_dwells = set()
        self.unique_values = set()
        self.gaps = []
        self.count = 0
        self.first_ts = self.last_ts = self.start_ts = self.value = self.last_index = None
        self.start_estimated = False
        self.head = CAPTURE_HEAD
        self.break_reason = ''

    def _close(self, end):
        self.segments.append((self.value, self.start_ts, end, end - self.start_ts))

    def append(self, index, value, ts, estimated=False, break_reason=''):
        if break_reason or value is None:
            if self.start_ts is not None:
                self._close(self.last_ts)
                self.start_ts = None
            self.break_reason = break_reason or self.break_reason or '通道缺值'
            self.head = GAP_HEAD if self.count else CAPTURE_HEAD
        if value is None:
            return
        if self.break_reason:
            if self.count:
                self.gaps.append({'left_index': self.last_index, 'right_index': index,
                                  'left_ms': self.last_ts, 'right_ms': ts,
                                  'left_value': self.value, 'right_value': value, 'reason': self.break_reason})
            self.break_reason = ''
        if self.start_ts is not None and value != self.value:
            self._close(ts)
            self.jumps.append((index, self.value, value, ts))
            self.dwell_starts[index] = self.start_ts
            if self.head:
                self.partial_dwells[index] = self.head
            elif estimated or self.start_estimated:
                self.estimated_dwells.add(index)
            self.start_ts = None
            self.head = ''
        if self.start_ts is None:
            self.value, self.start_ts, self.start_estimated = value, ts, estimated
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts, self.last_index = ts, index
        self.count += 1
        self.unique_values.add(value)

    def finish(self):
        if self.start_ts is not None:
            self._close(self.last_ts)
        return {'jumps': self.jumps, 'segments': self.segments, 'dwell_starts': self.dwell_starts,
                'partial_dwells': self.partial_dwells, 'estimated_dwells': self.estimated_dwells,
                'sample_count': self.count, 'time_range': (self.first_ts or 0, self.last_ts or 0),
                'inactive': not self.jumps, 'unique_values': sorted(self.unique_values)}


def gap_record(recording_id, channels, periods, channel_count=None):
    """Common gaps are the intersection of the channels' open, unsampled intervals."""
    count = len(channels) if channel_count is None else channel_count
    edges = Counter()
    details = []
    for name, gaps in channels.items():
        for gap in gaps:
            details.append({'channel': name, **gap})
            edges[gap['left_ms']] += 1
            edges[gap['right_ms']] -= 1
    shared = []
    active = 0
    previous = None
    for time, delta in sorted(edges.items()):
        if active == count and previous is not None and previous < time:
            shared.append({'left_ms': previous, 'right_ms': time, 'duration_ms': round(time - previous, 6)})
        active += delta
        previous = time
    return {'recording_id': recording_id, 'channels_total': count, 'period_ms': periods,
            'common_gaps': shared, 'channel_gaps': details}


def merge_recording(target, parts, index_offset, time_offset):
    """Keep recording-local evidence while assigning distinct indices for aggregation."""
    for name, part in parts.items():
        acc = target.setdefault(name, {'jumps': [], 'segments': [], 'dwell_starts': {}, 'partial_dwells': {},
                                       'estimated_dwells': set(), 'sample_count': 0, 'time_range': (0, 0),
                                       'inactive': True, 'unique_values': set()})
        if 'timestamps' in part:
            acc.setdefault('timestamps', []).extend(t + time_offset for t in part['timestamps'])
            acc.setdefault('values', []).extend(part['values'])
            acc['timestamp_unit'] = part['timestamp_unit']
        acc['jumps'].extend((i + index_offset, a, b, t + time_offset) for i, a, b, t in part['jumps'])
        acc['segments'].extend((v, a + time_offset, b + time_offset, d) for v, a, b, d in part['segments'])
        acc['dwell_starts'].update((i + index_offset, t + time_offset) for i, t in part['dwell_starts'].items())
        acc['partial_dwells'].update((i + index_offset, why) for i, why in part['partial_dwells'].items())
        acc['estimated_dwells'].update(i + index_offset for i in part['estimated_dwells'])
        if part['sample_count']:
            begin, end = part['time_range']
            acc['time_range'] = (acc['time_range'][0] if acc['sample_count'] else begin + time_offset, end + time_offset)
        acc['sample_count'] += part['sample_count']
        acc['inactive'] = acc['inactive'] and part['inactive']
        acc['unique_values'].update(part['unique_values'])
