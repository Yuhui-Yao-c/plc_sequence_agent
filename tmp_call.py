# -*- coding: utf-8 -*-
import io, re, sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
with io.open(p, encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# 谁在调用 ST10？（ST10( 调用形式 / : ST10 赋值目标）
hits = Counter()
first = {}
for ln, l in enumerate(lines, 1):
    for m in re.finditer(r'\b(ST1[0-9])\s*(?:\(|:=)', l):
        k = m.group(1)
        hits[k] += 1
        first.setdefault(k, (ln, l.strip()[:130]))
for k, v in hits.most_common(15):
    ln, s = first[k]
    print('  %-6s %4d  L%d: %s' % (k, v, ln, s))

# PPM_Distribution FB 的 interface/关键行
print('\n== PPM_Distribution pou 区间与签名 ==')
for ln, l in enumerate(lines, 1):
    if 'pou name="PPM_Distribution"' in l:
        for j in range(ln - 1, min(ln + 40, len(lines))):
            print('  L%d: %s' % (j + 1, lines[j].strip()[:120]))
        break
