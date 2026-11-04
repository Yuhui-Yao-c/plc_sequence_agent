# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd

p = r'source\超声\步序时长采集-XM2-L5-超声波-20260910.xls'
xl = pd.ExcelFile(p)
print('== sheets ==')
for s in xl.sheet_names:
    df = xl.parse(s, header=None, nrows=6)
    print('---', s, '形状', xl.parse(s, header=None).shape)
    print(df.head(4).to_string(max_colwidth=14))
