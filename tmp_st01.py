# -*- coding: utf-8 -*-
import io, re, sys
from collections import Counter

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'

# 1) ST01 的声明与类型
st01_decl = []
em10_body = []
in_em10 = False
depth = 0
scan_st = Counter()
hjxhx_decl = []
with io.open(p, encoding='utf-8', errors='ignore') as f:
    for ln, line in enumerate(f, 1):
        # ST01 声明（: 类型）
        for m in re.finditer(r'\b(ST\d{2})\s*:\s*([\w\.]+)', line):
            st01_decl.append((ln, m.group(1), m.group(2)))
        for m in re.finditer(r'\bHJXHX_ST(\d{2})\b', line):
            scan_st['HJXHX_ST' + m.group(1)] += 1
        # EM10 程序体
        if 'name="EM10_铜焊接工位1"' in line:
            in_em10 = True
            depth = 0
        if in_em10:
            em10_body.append((ln, line.rstrip()))
            depth += line.count('<') - line.count('</')
            if len(em10_body) > 400:
                in_em10 = False

print('== STxx 声明样本（前 25 条） ==')
for ln, name, ty in st01_decl[:25]:
    print('  L%d  %s : %s' % (ln, name, ty))
print('ST01 声明总数:', sum(1 for _, n, _ in st01_decl if n == 'ST01'))

print('\n== EM10_铜焊接工位1 程序体头部 60 行 ==')
for ln, l in em10_body[:60]:
    print('L%d: %s' % (ln, l[:150]))
