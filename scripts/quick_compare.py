"""快速对比：plot脚本 vs backtest脚本 信号生成（只跑前20个交易日）"""
import os, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj
from src.indicators.macd import calculate_macd
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.scoring import compute_total_score
from src.ml import ml_scorer
from src.market_state import get_market_state, is_tradeable, get_ml_min_threshold
from config.settings import RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT, ENHANCED_RULES_MIN

start_date, end_date = "20230601", "20260603"
lookback = pd.Timestamp(start_date) - pd.Timedelta(days=250)
lookfwd = pd.Timestamp(end_date) + pd.Timedelta(days=120)
fmt_start = lookback.strftime("%Y-%m-%d")
fmt_end = lookfwd.strftime("%Y-%m-%d")

# 加载数据
fetcher = DataFetcher()
sec_df = fetcher._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
sector_daily = {}
for _, row in sec_df.iterrows():
    df = fetcher._sql_to_df(
        "SELECT date, close, change_pct FROM sector_daily WHERE sector_name=? AND sector_type=? AND date>=? AND date<=? ORDER BY date",
        params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
    )
    if not df.empty:
        sector_daily[row["sector_name"]] = df

cons_df = fetcher._sql_to_df("SELECT sector_name, code, name FROM sector_constituents")
sector_constituents, stock_name_map = {}, {}
for _, row in cons_df.iterrows():
    n = row["sector_name"]
    sector_constituents.setdefault(n, set()).add(row["code"])
    stock_name_map[row["code"]] = row["name"]

all_codes = set()
for codes in sector_constituents.values():
    all_codes.update(codes)
stock_daily = {}
for code in all_codes:
    df = fetcher._sql_to_df(
        "SELECT date, open, high, low, close, volume, turnover_rate FROM stock_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        params=(code, fmt_start, fmt_end),
    )
    if not df.empty and len(df) >= 60:
        stock_daily[code] = df
fetcher.close()

# 交易日
all_dates = set()
for df in sector_daily.values():
    all_dates.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))

# CSV信号
df_csv = pd.read_csv("data/output/backtest_ml_20230601_20260603.csv")
df_csv["date"] = pd.to_datetime(df_csv["date"]).dt.strftime("%Y-%m-%d")
df_csv["code"] = df_csv["code"].astype(str).str.zfill(6)

# 加载CSV中前几个信号日的详细数据
csv_by_date = {}
for _, row in df_csv.iterrows():
    d = row["date"]
    csv_by_date.setdefault(d, []).append(row.to_dict())

# ML模型
ml_avail = ml_scorer.is_available()
print(f"ML模型可用: {ml_avail}")

def check_weekly_trend(df):
    if df is None or len(df) < 60: return True
    df_copy = df.copy()
    df_copy["date"] = pd.to_datetime(df_copy["date"])
    weekly = df_copy.resample("W", on="date").agg({"close":"last","high":"max","low":"min","open":"first","volume":"sum"}).dropna()
    if len(weekly) < 12: return True
    try:
        macd_w = calculate_macd(weekly)
        if macd_w is not None and len(macd_w) > 0:
            last = macd_w.iloc[-1]
            dif, dea = last.get("dif",0), last.get("dea",0)
            if pd.notna(dif) and pd.notna(dea): return dif > dea
    except: pass
    return True

# 只跑前30个有信号的交易日
results = []
signal_dates_found = 0
for di, date_str in enumerate(trading_dates):
    if signal_dates_found >= 30:
        break
    fmt_date = pd.Timestamp(date_str)
    sdata = {}
    for name, df in sector_daily.items():
        sub = df[pd.to_datetime(df["date"]) <= fmt_date]
        if len(sub) >= RPS_PERIOD: sdata[name] = sub
    if not sdata: continue
    try:
        rps_df = calculate_sector_rps(sdata, period=RPS_PERIOD)
        top_sectors = get_top_sectors(rps_df, top_n=RPS_TOP_N_STRICT)
    except: continue
    if rps_df.empty: continue
    rps_rank_map = dict(zip(rps_df["sector_name"], rps_df["rps_rank"]))
    code_to_sector = {}
    for name in top_sectors:
        for c in sector_constituents.get(name, set()):
            code_to_sector.setdefault(c, name)
    stock_dict = {}
    for code in code_to_sector:
        df = stock_daily.get(code)
        if df is None: continue
        sub = df[pd.to_datetime(df["date"]) <= fmt_date].copy()
        if len(sub) >= 60: stock_dict[code] = sub
    if not stock_dict: continue
    macd_codes = filter_by_macd(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)
    core_codes = macd_codes & zjtj_codes
    enhanced_codes = set()
    for code in core_codes:
        sub_df = stock_dict.get(code)
        if sub_df is not None and check_weekly_trend(sub_df):
            enhanced_codes.add(code)
    core_codes = enhanced_codes
    if not core_codes: continue
    
    past_returns = []
    for code, df in stock_dict.items():
        closes = df["close"].values
        if len(closes) >= 11:
            past_returns.append((closes[-1] / closes[-11] - 1) * 100)
    market_10d_past = np.mean(past_returns) if past_returns else 0
    market_state = get_market_state(market_10d_past)
    market_state_str = "weak_reduced" if not is_tradeable(market_state) else market_state

    day_signals = []
    for code in core_codes:
        df = stock_dict[code]
        sector = code_to_sector[code]
        rps_rank = rps_rank_map.get(sector, RPS_TOP_N)
        ml_val = None
        if ml_avail:
            try: ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
            except: pass
        ml_threshold = get_ml_min_threshold(market_state)
        if ml_val is not None and ml_val < ml_threshold: continue
        try:
            dm = calculate_macd(df)
            dk = calculate_kdj(df)
            dz = calculate_zjtj(df)
            scores = compute_total_score(dm, dk, dz, rps_rank, ml_score=ml_val)
            enh_info = check_all_enhanced_rules(df)
            if enh_info["rules_passed"] < ENHANCED_RULES_MIN: continue
        except: continue
        day_signals.append({
            "date": date_str, "code": code,
            "score_ml": ml_val or 0,
            "total_score": scores.get("total_score", 0),
            "max_score": scores.get("max_score", 100),
            "market_state": market_state_str,
        })
    if day_signals:
        signal_dates_found += 1
        results.extend(day_signals)

# 对比
print(f"\n=== Plot脚本信号(前30个有信号日) ===")
print(f"信号数: {len(results)}")

# 对比每个日期的信号
csv_dates_checked = 0
mismatch_count = 0
for sig in results:
    d = sig["date"]
    c = sig["code"]
    csv_sigs = csv_by_date.get(d, [])
    csv_codes = {s["code"] for s in csv_sigs}
    csv_match = [s for s in csv_sigs if s["code"] == c]
    
    if csv_match:
        csv_sig = csv_match[0]
        plot_ts = sig["total_score"]
        csv_ts = csv_sig["total_score"]
        plot_ml = sig["score_ml"]
        csv_ml = csv_sig["score_ml"]
        plot_ms = sig["max_score"]
        csv_ms = csv_sig["max_score"]
        if plot_ts != csv_ts or plot_ml != csv_ml or plot_ms != csv_ms:
            print(f"  差异 {d} {c}: plot(ts={plot_ts},ml={plot_ml},ms={plot_ms}) vs csv(ts={csv_ts},ml={csv_ml},ms={csv_ms})")
            mismatch_count += 1
    else:
        if csv_dates_checked < 5:
            print(f"  Plot独有 {d} {c}: ts={sig['total_score']}, ml={sig['score_ml']}")
    
    csv_dates_checked += 1

# CSV独有信号
csv_only = 0
for d, sigs in csv_by_date.items():
    for s in sigs:
        found = any(r["date"] == d and r["code"] == s["code"] for r in results)
        if not found and d <= results[-1]["date"] if results else True:
            csv_only += 1

print(f"\n不匹配信号: {mismatch_count}")
print(f"Plot独有信号: 见上")
print(f"ML可用性: {ml_avail}")
