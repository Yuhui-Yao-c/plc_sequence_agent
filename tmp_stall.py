# -*- coding: utf-8 -*-
import io, re, sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'

cnt = Counter()
samples = {}
st_decl = []
ppm_inst = []
with io.open(p, encoding='utf-8', errors='ignore') as f:
    for ln, line in enumerate(f, 1):
        for m in re.finditer(r'\b(ST\d{2})\b', line):
            cnt[m.group(1)] += 1
            samples.setdefault(m.group(1), (ln, line.strip()[:160]))
        # FB_StationPPM / iTS 实例声明
        if re.search(r':\s*(CATL_FB_StationPPM|iTS_stn_control)\b', line):
            ppm_inst.append((ln, line.strip()[:160]))

print('== STxx 总出现次数 ==')
for k, v in sorted(cnt.items(), key=lambda x: -x[1])[:25]:
    ln, s = samples[k]
    print('  %-8s %5d   L%d: %s' % (k, v, ln, s))

print('\n== StationPPM/iTS 实例声明（前 20） ==')
for ln, s in ppm_inst[:20]:
    print('  L%d: %s' % (ln, s))
