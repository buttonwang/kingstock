"""V1.1 信号生成瓶颈诊断脚本"""
import os, sys
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from config.settings import RPS_PERIOD, RPS_TOP_N_STRICT
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj

start_date, end_date = "20230601", "20260603"
fetcher = DataFetcher()
lookback = pd.Timestamp(start_date) - pd.Timedelta(days=250)
lookfwd = pd.Timestamp(end_date) + pd.Timedelta(days=120)
fmt_start = lookback.strftime("%Y-%m-%d")
fmt_end = lookfwd.strftime("%Y-%m-%d")

sec_df = fetcher._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
sector_daily = {}
for _, row in sec_df.iterrows():
    df = fetcher._sql_to_df(
        "SELECT date, close, change_pct FROM sector_daily WHERE sector_name=? AND sector_type=? "
        "AND date>=? AND date<=? ORDER BY date",
        params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
    )
    if not df.empty:
        sector_daily[row["sector_name"]] = df

cons_df = fetcher._sql_to_df("SELECT sector_name, code, name FROM sector_constituents")
sector_constituents = {}
for _, row in cons_df.iterrows():
    sector_constituents.setdefault(row["sector_name"], set()).add(row["code"])

stock_daily = {}
all_codes = set()
for codes in sector_constituents.values():
    all_codes.update(codes)
for code in all_codes:
    df = fetcher._sql_to_df(
        "SELECT date, open, high, low, close, volume, turnover_rate FROM stock_daily "
        "WHERE code=? AND date>=? AND date<=? ORDER BY date",
        params=(code, fmt_start, fmt_end),
    )
    if not df.empty and len(df) >= 60:
        stock_daily[code] = df
fetcher.close()

all_dates = set()
for df in sector_daily.values():
    all_dates.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates
                       if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))

# 统计各阶段的平均每日通过数
counts = {"rps_total": 0, "macd": 0, "zjtj": 0, "kdj": 0, 
          "macd_zjtj": 0, "zjtj_kdj": 0, "all_three": 0}
days_sampled = 0

for di, date_str in enumerate(trading_dates):
    if di % 20 != 0:  # 每20天抽样
        continue
    
    days_sampled += 1
    fmt_date = pd.Timestamp(date_str)
    
    sdata = {}
    for name, df in sector_daily.items():
        sub = df[pd.to_datetime(df["date"]) <= fmt_date]
        if len(sub) >= RPS_PERIOD:
            sdata[name] = sub
    if not sdata:
        continue
    
    try:
        rps_df = calculate_sector_rps(sdata, period=RPS_PERIOD)
        top_sectors = get_top_sectors(rps_df, top_n=RPS_TOP_N_STRICT)
    except Exception:
        continue
    
    code_to_sector = {}
    for name in top_sectors:
        for c in sector_constituents.get(name, set()):
            code_to_sector.setdefault(c, name)
    
    stock_dict = {}
    for code in code_to_sector:
        df = stock_daily.get(code)
        if df is None:
            continue
        sub = df[pd.to_datetime(df["date"]) <= fmt_date].copy()
        if len(sub) >= 60:
            stock_dict[code] = sub
    
    counts["rps_total"] += len(stock_dict)
    
    macd = filter_by_macd(stock_dict) if stock_dict else set()
    zjtj = filter_by_zjtj(stock_dict) if stock_dict else set()
    kdj = filter_by_kdj(stock_dict) if stock_dict else set()
    
    counts["macd"] += len(macd)
    counts["zjtj"] += len(zjtj)
    counts["kdj"] += len(kdj)
    counts["macd_zjtj"] += len(macd & zjtj)
    counts["zjtj_kdj"] += len(zjtj & kdj)
    counts["all_three"] += len(macd & zjtj & kdj)

print(f"\n抽样天数: {days_sampled}")
print(f"{'过滤阶段':<20} {'总计通过':<10} {'日均通过':<10}")
print("-" * 40)
print(f"{'RPS筛选后':<20} {counts['rps_total']:<10} {counts['rps_total']/days_sampled:<10.1f}")
print(f"{'MACD':<20} {counts['macd']:<10} {counts['macd']/days_sampled:<10.1f}")
print(f"{'ZJTJ':<20} {counts['zjtj']:<10} {counts['zjtj']/days_sampled:<10.1f}")
print(f"{'KDJ':<20} {counts['kdj']:<10} {counts['kdj']/days_sampled:<10.1f}")
print(f"{'MACD∩ZJTJ (V1.0)':<20} {counts['macd_zjtj']:<10} {counts['macd_zjtj']/days_sampled:<10.1f}")
print(f"{'ZJTJ∩KDJ (V1.1)':<20} {counts['zjtj_kdj']:<10} {counts['zjtj_kdj']/days_sampled:<10.1f}")
print(f"{'三选三':<20} {counts['all_three']:<10} {counts['all_three']/days_sampled:<10.1f}")
print(f"\n{'ZJTJ∩KDJ 相对 MACD∩ZJTJ':<40} {counts['zjtj_kdj']/max(counts['macd_zjtj'],1):.2f}x")
