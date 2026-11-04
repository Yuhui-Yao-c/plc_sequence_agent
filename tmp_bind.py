# -*- coding: utf-8 -*-
import io, re, sys

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
with io.open(p, encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# 找 EM10_铜焊接工位1 与 EM01_机台上芯工位 的 pou 全区间，并检查体内是否引用 ST10/ST01
def pou_range(name):
    start = None
    for ln, l in enumerate(lines, 1):
        if 'pou name="%s"' % name in l:
            start = ln
        elif start and '</pou>' in l and ln > start:
            return start, ln
    return start, None

for nm in ['EM10_铜焊接工位1', 'EM01_机台上芯工位', 'EM13_A贴胶工位']:
    s, e = pou_range(nm)
    stref = []
    if s and e:
        m = re.search(r'EM(\d{2})', nm)
        stn = 'ST' + m.group(1)
        for ln in range(s, e):
            if re.search(r'\b%s\b' % stn, lines[ln]):
                stref.append((ln, lines[ln].strip()[:120]))
    print('[%s] pou L%d-L%d, 体内 %s 引用 %s: %d 处' % (nm, s, e, stn, stn, len(stref)))
    for ln, l in stref[:6]:
        print('    L%d: %s' % (ln, l))
