# -*- coding: utf-8 -*-
import io, sys

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
with io.open(p, encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()
print('总行数', len(lines))
for i in range(794615, 794860):
    print(i + 1, lines[i].rstrip()[:120])
