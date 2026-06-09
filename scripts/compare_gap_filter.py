"""V2.2 跳空过滤阈值对比回测

对比3组参数:
  - 无过滤 (baseline)
  - 3% 跳空过滤
  - 5% 跳空过滤

信号只生成一次（耗时步骤），然后分别跑3组组合模拟。

Version: 1.0
Date: 2026-06-08
"""

import os, sys
import numpy as np
import pandas as pd
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.market_state import get_market_state, is_tradeable
from config.settings import RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.indicators.macd import calculate_macd, is_macd_buy_signal
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.scoring import compute_total_score
from src.ml import ml_scorer
from scripts.v2_0_execution import simulate_v20_portfolio, V2_0_CONFIG

start_date, end_date = "20230601", "20260603"

print("=" * 80)
print("66大顺 V2.2 — 跳空过滤阈值对比回测")
print("=" * 80)
print("对比组: 无过滤(baseline) | 3%阈值 | 5%阈值")
print()

# ── 加载数据 ──
fetcher = DataFetcher()
lookback = pd.Timestamp(start_date) - pd.Timedelta(days=250)
lookfwd = pd.Timestamp(end_date) + pd.Timedelta(days=120)
fmt_start = lookback.strftime("%Y-%m-%d")
fmt_end = lookfwd.strftime("%Y-%m-%d")

print("加载板块数据...")
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

print("加载成分股数据...")
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
        "SELECT date, open, high, low, close, volume, turnover_rate FROM stock_daily "
        "WHERE code=? AND date>=? AND date<=? ORDER BY date",
        params=(code, fmt_start, fmt_end),
    )
    if not df.empty and len(df) >= 60:
        stock_daily[code] = df
fetcher.close()
print(f"加载完成: {len(sector_daily)}板块, {len(stock_daily)}只个股")

# 交易日列表
all_dates_set = set()
for df in sector_daily.values():
    all_dates_set.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates_set
                       if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))
print(f"交易日数: {len(trading_dates)}")

# ── 信号生成（只做一次） ──
from config.settings import SCORE_THRESHOLD_STRONG, SCORE_THRESHOLD_CHOPPY, ENHANCED_RULES_MIN

def check_weekly_trend(df):
    if df is None or len(df) < 60:
        return True
    df_copy = df.copy()
    df_copy["date"] = pd.to_datetime(df_copy["date"])
    weekly = df_copy.resample("W", on="date").agg({
        "close": "last", "high": "max", "low": "min",
        "open": "first", "volume": "sum",
    }).dropna()
    if len(weekly) < 12:
        return True
    try:
        macd_w = calculate_macd(weekly)
        if macd_w is not None and len(macd_w) > 0:
            last = macd_w.iloc[-1]
            dif = last.get("dif", 0)
            dea = last.get("dea", 0)
            if pd.notna(dif) and pd.notna(dea):
                return dif > dea
    except Exception:
        pass
    return True

ml_avail = ml_scorer.is_available()
total_dates = len(trading_dates)
results_v20 = []

TA_ML_MIN = V2_0_CONFIG['track_a']['ml_min']
TA_ENH_MIN = V2_0_CONFIG['track_a']['enhanced_rules_min']
TB_ML_MIN = V2_0_CONFIG['track_b']['ml_min']
TB_ENH_MIN = V2_0_CONFIG['track_b']['enhanced_rules_min']

print(f"\n生成V2.2信号（仅一次）...")
for di, date_str in enumerate(trading_dates):
    if di % max(1, total_dates // 10) == 0:
        print(f"  信号生成 {di}/{total_dates} ({100*di//total_dates}%)")

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
    if rps_df.empty:
        continue

    rps_rank_map = dict(zip(rps_df["sector_name"], rps_df["rps_rank"]))
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
    if not stock_dict:
        continue

    macd_codes = filter_by_macd(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)

    past_returns = []
    for code, df in stock_dict.items():
        closes = df["close"].values
        if len(closes) >= 11:
            past_returns.append((closes[-1] / closes[-11] - 1) * 100)
    market_10d_past = np.mean(past_returns) if past_returns else 0
    market_state = get_market_state(market_10d_past)
    if not is_tradeable(market_state):
        market_state_str = "weak_reduced"
    else:
        market_state_str = market_state

    for code in stock_dict:
        df = stock_dict[code]
        sector = code_to_sector.get(code)
        if sector is None:
            continue
        rps_rank = rps_rank_map.get(sector, RPS_TOP_N)

        ml_val = None
        if ml_avail:
            try:
                ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
            except Exception:
                pass

        try:
            df_macd = calculate_macd(df)
        except Exception:
            continue
        macd_strict = is_macd_buy_signal(df_macd)

        try:
            enh_info = check_all_enhanced_rules(df)
        except Exception:
            enh_info = {"rules_passed": 0, "rules_total": 3}
        rules_passed = enh_info["rules_passed"]
        weekly_ok = check_weekly_trend(df)
        in_zjtj = code in zjtj_codes
        score_ml = int(round(ml_val)) if ml_val is not None else 0

        if (macd_strict and in_zjtj and weekly_ok
                and rules_passed >= TA_ENH_MIN
                and (ml_val is not None and ml_val >= TA_ML_MIN)):
            try:
                dk = calculate_kdj(df)
                dz = calculate_zjtj(df)
                scores = compute_total_score(df_macd, dk, dz, rps_rank, ml_score=ml_val)
            except Exception:
                continue
            results_v20.append({
                "date": date_str, "code": code,
                "score_ml": score_ml,
                "total_score": scores.get("total_score", 0),
                "market_state": market_state_str,
                "signal_track": 0,
            })
            continue

        if (in_zjtj and weekly_ok
                and rules_passed >= TB_ENH_MIN
                and (ml_val is not None and ml_val >= TB_ML_MIN)
                and rps_rank <= 5):
            try:
                dk = calculate_kdj(df)
                dz = calculate_zjtj(df)
                scores = compute_total_score(df_macd, dk, dz, rps_rank, ml_score=ml_val)
            except Exception:
                continue
            results_v20.append({
                "date": date_str, "code": code,
                "score_ml": score_ml,
                "total_score": scores.get("total_score", 0),
                "market_state": market_state_str,
                "signal_track": 1,
            })

df_v20 = pd.DataFrame(results_v20)
print(f"\n信号总数: {len(df_v20)}")
if not df_v20.empty:
    for t, c in df_v20['signal_track'].value_counts().sort_index().items():
        print(f"  Track {'A' if t==0 else 'B'}: {c}")

# ── 3组对比模拟 ──
gap_thresholds = [
    ("无过滤(baseline)", None),
    ("3%跳空过滤", 0.03),
    ("5%跳空过滤", 0.05),
]

results = {}
for label, gap_pct in gap_thresholds:
    print(f"\n模拟: {label}...")
    r = simulate_v20_portfolio(df_v20, stock_daily, trading_dates,
                                gap_filter_pct=gap_pct)
    results[label] = r
    print(f"  年化={r.get('ann_return',0):.2f}%  回撤={r.get('max_drawdown',0):.2f}%  "
          f"夏普={r.get('sharpe',0):.2f}  交易={r.get('total_trades',0)}  "
          f"胜率={r.get('win_rate',0):.1f}%  跳过={r.get('gap_skipped',0)}")

# ── 对比表格 ──
print(f"\n{'='*90}")
print("V2.2 跳空过滤阈值对比结果")
print(f"{'='*90}")

header = f"{'指标':<18}"
for label, _ in gap_thresholds:
    header += f" {label:<22}"
print(header)
print("-" * 90)

metrics = [
    ("年化收益%", "ann_return", ".2f"),
    ("总收益%", "total_return", ".2f"),
    ("最大回撤%", "max_drawdown", ".2f"),
    ("夏普比率", "sharpe", ".2f"),
    ("胜率%", "win_rate", ".1f"),
    ("盈亏比", "profit_ratio", ".2f"),
    ("利润因子", "profit_factor", ".2f"),
    ("交易次数", "total_trades", "d"),
    ("跳空跳过", "gap_skipped", "d"),
]

for name, key, fmt in metrics:
    row = f"{name:<18}"
    for label, _ in gap_thresholds:
        val = results[label].get(key, 0)
        row += f" {val:{fmt}:>20}"
    print(row)

# 各轨道对比
print(f"\n{'─'*90}")
print("各轨道分项统计:")
print(f"{'─'*90}")
track_names = {0: "Track A(核心)", 1: "Track B(扩容)"}

for tid in [0, 1]:
    print(f"\n  {track_names[tid]}:")
    row_h = f"    {'指标':<14}"
    for label, _ in gap_thresholds:
        row_h += f" {label:<22}"
    print(row_h)
    print(f"    {'-'*80}")

    ts = results[list(results.keys())[0]].get("track_stats", {})
    for metric_name, metric_key in [("交易数", "count"), ("胜率%", "win_rate"), ("平均回报%", "avg_return")]:
        row = f"    {metric_name:<14}"
        for label, _ in gap_thresholds:
            stats = results[label].get("track_stats", {}).get(tid, {})
            val = stats.get(metric_key, 0)
            if metric_key == "avg_return":
                row += f" {val:>20.2f}"
            elif metric_key == "win_rate":
                row += f" {val:>20.1f}"
            else:
                row += f" {val:>20}"
        print(row)

# 退出原因对比
print(f"\n{'─'*90}")
print("退出原因分布对比:")
print(f"{'─'*90}")
all_reasons = set()
for label, _ in gap_thresholds:
    all_reasons.update(results[label].get("exit_reasons", {}).keys())

for reason in sorted(all_reasons):
    row = f"  {reason:<22}"
    for label, _ in gap_thresholds:
        cnt = results[label].get("exit_reasons", {}).get(reason, 0)
        row += f" {cnt:>20}"
    print(row)

# ── 结论 ──
print(f"\n{'='*90}")
print("结论分析:")
print(f"{'='*90}")

baseline = results["无过滤(baseline)"]
gap3 = results["3%跳空过滤"]
gap5 = results["5%跳空过滤"]

# 计算差异
d3_ann = gap3["ann_return"] - baseline["ann_return"]
d3_dd = gap3["max_drawdown"] - baseline["max_drawdown"]
d3_sr = gap3["sharpe"] - baseline["sharpe"]

d5_ann = gap5["ann_return"] - baseline["ann_return"]
d5_dd = gap5["max_drawdown"] - baseline["max_drawdown"]
d5_sr = gap5["sharpe"] - baseline["sharpe"]

print(f"  3%过滤 vs baseline: 年化{d3_ann:+.2f}%, 回撤{d3_dd:+.2f}%, 夏普{d3_sr:+.2f}")
print(f"  5%过滤 vs baseline: 年化{d5_ann:+.2f}%, 回撤{d5_dd:+.2f}%, 夏普{d5_sr:+.2f}")

# 推荐
best_label = "无过滤"
best_sharpe = baseline["sharpe"]
if gap3["sharpe"] > best_sharpe:
    best_label = "3%"
    best_sharpe = gap3["sharpe"]
if gap5["sharpe"] > best_sharpe:
    best_label = "5%"
    best_sharpe = gap5["sharpe"]

print(f"\n  夏普比率最优: {best_label}")

# 综合评分 = 年化/回撤 * 0.6 + 夏普 * 0.4
def score(r):
    calmar = r["ann_return"] / max(r["max_drawdown"], 0.01)
    return calmar * 0.6 + r["sharpe"] * 0.4

scores = {label: score(results[label]) for label, _ in gap_thresholds}
best_overall = max(scores, key=scores.get)
print(f"  综合评分最优: {best_overall}")
print(f"  (评分 = Calmar*0.6 + Sharpe*0.4)")
for label, s in scores.items():
    print(f"    {label}: {s:.2f}")

print(f"\n{'='*90}")
