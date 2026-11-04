# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd
import re
from collections import Counter

p = r'source\超声\步序时长采集-XM2-L5-超声波-20260910.xls'
df = xl = pd.read_excel(p, sheet_name='时序拆解', header=1)
df.columns = [str(c).strip() for c in df.columns]
print('列名:', list(df.columns))

# 工位向下填充
work = df.iloc[:, 0].ffill()
uniq = [w for w in work.dropna().unique() if str(w).strip() and str(w) != 'nan']
print('\n== 人工表工位唯一值 (%d) ==' % len(uniq))
for w in uniq:
    print('  ', str(w).strip())

# 开始标签前缀分布
tagcol = [c for c in df.columns if '开始标签' in str(c)]
if tagcol:
    tags = df[tagcol[0]].dropna().astype(str)
    pref = Counter(re.match(r'[A-Za-z_]+\d*', t).group(0) if re.match(r'[A-Za-z_]+\d*', t) else t[:6] for t in tags)
    print('\n== 开始标签前缀分布 ==')
    for k, v in pref.most_common(20):
        print('  %-12s %d' % (k, v))
