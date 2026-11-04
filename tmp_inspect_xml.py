# -*- coding: utf-8 -*-
import re, io, sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
progs, pous = [], []
with io.open(p, encoding='utf-8', errors='ignore') as f:
    for line in f:
        m = re.search(r'<program name="([^"]+)"', line)
        if m:
            progs.append(m.group(1))
        m = re.search(r'<pou name="([^"]+)" pouType="(\w+)"', line)
        if m:
            pous.append((m.group(2), m.group(1)))

print('== programs (%d) ==' % len(progs))
for x in progs:
    print(' ', x)

d = defaultdict(list)
for t, n in pous:
    d[t].append(n)
print('== POUs by type ==')
for t, ns in d.items():
    print('[%s] %d 个' % (t, len(ns)))
    for n in ns[:60]:
        print('   ', n)
    if len(ns) > 60:
        print('    ... 共 %d' % len(ns))
