# -*- coding: utf-8 -*-
import io, re, sys

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
with io.open(p, encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# 1) ST10_CU1 的类型（EM10 程序 interface 内）
print('== L49525-L49545 ==')
for i in range(49524, 49545):
    print('  L%d: %s' % (i + 1, lines[i].strip()[:120]))

# 2) PackMLMode[10] 赋值上下文（L826070-826085）
print('== L826070-L826090 ==')
for i in range(826069, 826090):
    print('  L%d: %s' % (i + 1, lines[i].strip()[:130]))

# 3) 全局搜 PackMLMode 数组声明与下标含义
print('== PackMLMode 声明 ==')
for ln, l in enumerate(lines, 1):
    if re.search(r'PackMLMode\s*:\s*ARRAY', l):
        print('  L%d: %s' % (ln, l.strip()[:140]))
        break

# 4) 人工表 CU焊接1 行的完整开始/结束标签（未截断）
import pandas as pd
df2 = pd.read_excel(r'source\超声\步序时长采集-XM2-L5-超声波-20260910.xls', sheet_name='时序拆解', header=1)
df2.columns = [str(c).strip() for c in df2.columns]
m2 = df2.iloc[:, 0].astype(str).str.contains('CU焊接1', na=False)
print('== CU焊接1 全部行标签 ==')
for _, r in df2[m2].iterrows():
    print('  %-20s | %-10s | %s | %s' % (str(r.iloc[0])[:20], str(r.iloc[1])[:10], r.iloc[8], r.iloc[9]))
