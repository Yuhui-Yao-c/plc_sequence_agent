# -*- coding: utf-8 -*-
import io, re, sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
with io.open(p, encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# ST10 在代码(STCode)中的出现样本 vs HMI绑定(<variable>)的出现
code_hits, hmi_hits = [], []
for ln, l in enumerate(lines, 1):
    if 'ST10' in l:
        if '<STCode' in l or 'ST10.' in l and '<variable>' in l:
            hmi_hits.append((ln, l.strip()[:110]))
        elif 'ST10' in l:
            code_hits.append((ln, l.strip()[:130]))
print('ST10 相关行总数: code=%d hmi=%d' % (len(code_hits), len(hmi_hits)))
print('\n== 代码样本 ==')
for ln, s in code_hits[:14]:
    print('  L%d: %s' % (ln, s))
print('\n== HMI 绑定样本 ==')
for ln, s in hmi_hits[:6]:
    print('  L%d: %s' % (ln, s))

# 看 GVL 里 ST01..ST20 附近的 GrpComment（该 GVL 的名字）——回溯 L794644 之前最近的 <resource>/<globalVars> 名
print('\n== GVL 名字回溯 ==')
for ln in range(794643, 794400, -1):
    m = re.search(r'<globalVars name="([^"]+)"', lines[ln]) or re.search(r'<resource name="([^"]+)"', lines[ln]) or re.search(r'<program name="([^"]+)"', lines[ln])
    if m:
        print('  L%d: %s' % (ln + 1, m.group(1)))
        break
