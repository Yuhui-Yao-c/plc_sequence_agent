# -*- coding: utf-8 -*-
import io, re, sys

sys.stdout.reconfigure(encoding='utf-8')
p = r'source\超声\XM22PPM_L5B_20260826.xml'
with io.open(p, encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# 1) 人工表焊接相关行的标签全文
import pandas as pd
df = pd.read_excel(r'source\超声\步序时长采集-XM2-L5-超声波-20260910.xls', sheet_name='时序拆解', header=1)
df.columns = [str(c).strip() for c in df.columns]
mask = df.iloc[:, 0].astype(str).str.contains('焊接', na=False)
print('== 人工表焊接行的 工位/动作/开始标签/结束标签（前 14 行） ==')
for _, r in df[mask].head(14).iterrows():
    print('  %-22s | %-8s | %s | %s' % (
        str(r.iloc[0])[:22], str(r.iloc[1])[:8], str(r.iloc[8])[:44], str(r.iloc[9])[:44]))

# 2) HJXHX_ST00.. 名单与文档
print('\n== HJXHX_ST 名单（GVL 声明区 L794800-795050 内的 name+documentation） ==')
zone = lines[794800:795100]
cur = None
for l in zone:
    m = re.search(r'<variable name="(HJXHX_ST\d{2})">', l)
    if m:
        cur = m.group(1)
    m2 = re.search(r'<xhtml[^>]*>([^<]{1,40})</xhtml>', l)
    if m2 and cur:
        print('  %-14s %s' % (cur, m2.group(1)))
        cur = None

# 3) EM10 程序体内 ST10 调用证据：找 'ST10' 在 153 万行中的 program body 上下文
print('\n== ST10. 成员访问样本（前 12 个不同样本） ==')
seen = set()
n = 0
for ln, l in enumerate(lines, 1):
    if 'ST10.' in l:
        s = l.strip()[:120]
        key = s[:60]
        if key not in seen:
            seen.add(key)
            print('  L%d: %s' % (ln, s))
            n += 1
        if n >= 12:
            break
