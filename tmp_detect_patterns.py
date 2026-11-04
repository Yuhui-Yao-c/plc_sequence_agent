# -*- coding: utf-8 -*-
"""探测四台机器的状态变量命名模式（工位步号载体）。只读源码头部采样。"""
import io, re, sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')

SOURCES = {
    '超声波':  r'source\超声\XM22PPM_L5B_20260826.xml',
    '配对机':  r'source\配对机\XM2-L8A-配对机.xml',
    '转接片':  r'source\转接片\1.JMLEDQA10_CATL_厦门时代_转接片_20260813.xml',
    '包mylar': r'source\包mylar\L8A1-Mylar PLC程序_202608251035.xml',
}

# 常见步号载体模式采样
PATS = [
    (r'\b([A-Za-z_]\w*)\.AutoStep\s*\[\s*\d+\s*\]', 'AutoStep数组'),
    (r'\b([A-Za-z_]\w*)\.iCmdStep\b', 'iCmdStep'),
    (r'\b([A-Za-z_]\w*)\.iStep\b', 'iStep'),
    (r'\b([A-Za-z_]\w*)\.Step\b', 'Step'),
    (r'\b([A-Za-z_]\w*)\.AutoStep\b', 'AutoStep标量'),
]

for name, p in SOURCES.items():
    cnt = {label: Counter() for _, label in PATS}
    with io.open(p, encoding='utf-8', errors='ignore') as f:
        for line in f:
            for pat, label in PATS:
                for m in re.finditer(pat, line):
                    cnt[label][m.group(1)] += 1
    print('===', name)
    for label, c in cnt.items():
        if c:
            tops = ', '.join('%s×%d' % (k, v) for k, v in c.most_common(6))
            print('   %-12s %d种根 / %d次  例: %s' % (label, len(c), sum(c.values()), tops))
