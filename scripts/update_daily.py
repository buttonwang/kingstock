"""快速更新 stock_daily + sector_daily 数据（跳过板块列表抓取）"""
import sys, os, time
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

import pandas as pd
from src.data_fetcher import DataFetcher

fetcher = DataFetcher()

# 找出所有需要更新的股票代码（从 sector_constituents 获取）
cons = fetcher._sql_to_df("SELECT DISTINCT code FROM sector_constituents")
codes = cons["code"].tolist()
print(f"共 {len(codes)} 只股票需要更新 stock_daily")

# 目标日期
today = pd.Timestamp.now().strftime("%Y%m%d")
today_dash = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
print(f"目标日期: {today}")

# 找出尚未更新到目标日期的股票
updated = fetcher._sql_to_df(f"SELECT DISTINCT code FROM stock_daily WHERE date='{today_dash}'")
updated_codes = set(updated["code"].tolist())
pending_codes = [c for c in codes if c not in updated_codes]
print(f"已更新 {len(updated_codes)} 只，还需更新 {len(pending_codes)} 只")

if not pending_codes:
    print("stock_daily 全部已更新")
else:
    print(f"增量更新 stock_daily: 目标 {today}, 剩余 {len(pending_codes)} 只")
    ok = 0
    fail = 0
    t0 = time.time()
    # 从最新日期的前一天开始拉取（确保覆盖）
    start_date = "20260606"  # 周六，API会自动跳到下一个交易日
    for i, code in enumerate(pending_codes, 1):
        try:
            df = fetcher.get_stock_daily(code, start_date, today)
            if not df.empty:
                ok += 1
        except Exception as e:
            fail += 1
        if i % 50 == 0 or i == len(pending_codes):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(pending_codes) - i) / rate if rate > 0 else 0
            print(f"  进度: {i}/{len(pending_codes)} (成功{ok}, 失败{fail}) 耗时{elapsed:.0f}s, 预计还需{eta:.0f}s")

# 更新 sector_daily（尝试，失败则跳过）
print("\n更新 sector_daily...")
sectors = fetcher._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
r2 = fetcher._sql_to_df("SELECT MAX(date) as d FROM sector_daily")
sec_max = str(r2.iloc[0]["d"])[:10]
sec_start = (pd.Timestamp(sec_max) + pd.Timedelta(days=1)).strftime("%Y%m%d")

if sec_start > today:
    print("sector_daily 已是最新")
else:
    print(f"增量更新 sector_daily: {sec_start} ~ {today}")
    sec_ok = 0
    sec_fail = 0
    for i, (_, row) in enumerate(sectors.iterrows(), 1):
        try:
            df = fetcher.get_sector_daily(
                row["sector_name"], row["sector_type"], sec_start, today
            )
            if not df.empty:
                sec_ok += 1
        except Exception:
            sec_fail += 1
        if i % 20 == 0 or i == len(sectors):
            print(f"  进度: {i}/{len(sectors)} (成功{sec_ok}, 失败{sec_fail})")

# 验证
r1 = fetcher._sql_to_df("SELECT MAX(date) as d FROM stock_daily")
r2 = fetcher._sql_to_df("SELECT MAX(date) as d FROM sector_daily")
print(f"\n更新后: stock_daily={str(r1.iloc[0]['d'])[:10]}, sector_daily={str(r2.iloc[0]['d'])[:10]}")

fetcher.close()
print("完成!")
