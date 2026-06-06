# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding='utf-8')

from src.data_fetcher import DataFetcher

f = DataFetcher()

# 测试1: 获取行业板块列表
df = f.get_sector_list('industry')
print(f'行业板块: {len(df)}个')
print(df.head())
print()

# 测试2: 获取概念板块列表
df2 = f.get_sector_list('concept')
print(f'概念板块: {len(df2)}个')
print(df2.head())
print()

# 测试3: 获取第一个行业板块的成分股
if not df.empty:
    first_sector = df.iloc[0]['sector_name']
    cons = f.get_sector_constituents(first_sector, 'industry')
    print(f'板块 "{first_sector}" 成分股: {len(cons)}只')
    print(cons.head())
    print()

# 测试4: 获取板块历史行情
if not df.empty:
    first_sector = df.iloc[0]['sector_name']
    daily = f.get_sector_daily(first_sector, 'industry', '20250101', '20250601')
    print(f'板块 "{first_sector}" 日线数据: {len(daily)}条')
    print(daily.head())

f.close()
