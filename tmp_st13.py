# -*- coding: utf-8 -*-
import io, re, sys

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
with io.open(p, encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# ST13.AutoStep 的步序代码样本（case 分支）
samples = []
for ln, l in enumerate(lines, 1):
    if 'ST13.AutoStep' in l and ('CASE' in l.upper() or ':=' in l or 'IF' in l.upper()):
        samples.append((ln, l.strip()[:140]))
    if len(samples) >= 16:
        break
print('== ST13.AutoStep 代码样本 ==')
for ln, s in samples:
    print('  L%d: %s' % (ln, s))

# 人工表 A贴胶 的动作行与动作号
import pandas as pd
df2 = pd.read_excel(r'source\超声\步序时长采集-XM2-L5-超声波-20260910.xls', sheet_name='时序拆解', header=1)
df2.columns = [str(c).strip() for c in df2.columns]
m3 = df2.iloc[:, 0].astype(str).str.strip().eq('A贴胶')
print('\n== 人工表 A贴胶 行（工位/动作/动作号/开始标签/结束标签） ==')
for _, r in df2[m3].iterrows():
    print('  %-8s | 动作号%-6s | %s | %s' % (str(r.iloc[1])[:8], str(r.iloc[10])[:6], r.iloc[8], r.iloc[9]))
