"""66大顺 V2.2 回测脚本 — V1.0 + V1.1 双轨道融合 【最终版】

轨道A: V1.0 Core (MACD严格金叉∩ZJTJ + 周线 + ML≥10 + 增强≥1 → Pure Hold 8%)
轨道B: V1.1 扩容 (ZJTJ-only + 周线 + ML≥13 + 增强≥2 + RPS≤5 → 动态退出 5%)

年化: 19.80% | 回撤: 10.61% | 夏普: 1.40

Version: 2.2 (final)
Date: 2026-06-07
"""

import os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.market_state import get_market_state, is_tradeable, get_ml_min_threshold
from config.settings import RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj
from src.indicators.macd import calculate_macd, is_macd_buy_signal
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.portfolio_manager import simulate_pure_portfolio
from scripts.v2_0_execution import simulate_v20_portfolio, V2_0_CONFIG

start_date, end_date = "20230601", "20260603"
print("=" * 80)
print("66大顺 V2.2 V1.0+V1.1 双轨道融合方案回测【最终版】")
print("=" * 80)
print(f"\n配置摘要:")
print(f"  【轨道A】MACD严格金叉∩ZJTJ + 周线 + ML≥10 + 增强≥1 → Pure Hold (8%仓位)")
print(f"  【轨道B】ZJTJ-only + 周线 + ML≥13 + 增强≥2 + RPS≤5 → 动态退出 (5%仓位)")
print(f"    动态退出: -8%硬止损, +15%激活, -5%回撤止盈, max 12天")
print(f"  每日上限: 强市(A:4,B:3) | 震荡(A:3,B:2) | 弱市(0,0)")
print(f"  市场门控: 强市→双轨全开 | 震荡→双轨半仓 | 弱市→不交易")
print()

# ── 加载数据 ──
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

# 基准指数
import akshare as ak
index_map = {"sh000300": "沪深300", "sh000906": "中证800"}
benchmark_data = {}
start_ts = pd.Timestamp(start_date)
end_ts = pd.Timestamp(end_date)
for symbol, name in index_map.items():
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        df["date"] = pd.to_datetime(df["date"])
        df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)]
        if not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            benchmark_data[name] = df
            print(f"{name}: {len(df)} rows")
    except Exception as e:
        print(f"{name} error: {e}")

# 交易日列表
all_dates = set()
for df in sector_daily.values():
    all_dates.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates
                       if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))
print(f"交易日数: {len(trading_dates)}")

# ── 信号生成（V2.0 双轨道分配） ──
from src.scoring import compute_total_score
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.ml import ml_scorer
from config.settings import (
    RPS_TOP_N_STRICT, ML_SCORE_MIN_THRESHOLD,
    SCORE_THRESHOLD_STRONG, SCORE_THRESHOLD_CHOPPY,
    ENHANCED_RULES_MIN,
)


def check_weekly_trend(df):
    """周线MACD多头确认"""
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

results_v20 = []   # V2.0 signals with track
results_v10 = []   # V1.0 signals (for comparison)

# V2.0 轨道参数
TA_ML_MIN = V2_0_CONFIG['track_a']['ml_min']     # 10
TA_ENH_MIN = V2_0_CONFIG['track_a']['enhanced_rules_min']  # 1
TB_ML_MIN = V2_0_CONFIG['track_b']['ml_min']     # 13
TB_ENH_MIN = V2_0_CONFIG['track_b']['enhanced_rules_min']  # 2

for di, date_str in enumerate(trading_dates):
    if di % max(1, total_dates // 20) == 0:
        print(f"回测 {di}/{total_dates} ({100*di//total_dates}%)")

    fmt_date = pd.Timestamp(date_str)

    # RPS
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

    # 构建当日stock_dict
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

    # 获取各指标通过的codes
    macd_codes = filter_by_macd(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)

    # 市场状态
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

    # 为每个代码做双轨道信号判定
    for code in stock_dict:
        df = stock_dict[code]
        sector = code_to_sector.get(code)
        if sector is None:
            continue
        rps_rank = rps_rank_map.get(sector, RPS_TOP_N)

        # ML评分
        ml_val = None
        if ml_avail:
            try:
                ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
            except Exception:
                pass

        # 计算MACD状态
        try:
            df_macd = calculate_macd(df)
        except Exception:
            continue
        macd_strict = is_macd_buy_signal(df_macd)

        # 增强规则
        try:
            enh_info = check_all_enhanced_rules(df)
        except Exception:
            enh_info = {"rules_passed": 0, "rules_total": 3}

        rules_passed = enh_info["rules_passed"]

        # 周线趋势
        weekly_ok = check_weekly_trend(df)

        in_zjtj = code in zjtj_codes
        score_ml = int(round(ml_val)) if ml_val is not None else 0

        # ── 轨道A: V1.0 核心 ──
        if (macd_strict and in_zjtj
                and weekly_ok
                and rules_passed >= TA_ENH_MIN
                and (ml_val is not None and ml_val >= TA_ML_MIN)):
            try:
                dk = calculate_kdj(df)
                dz = calculate_zjtj(df)
                scores = compute_total_score(df_macd, dk, dz, rps_rank, ml_score=ml_val)
            except Exception:
                continue
            results_v20.append({
                "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
                "score_ml": score_ml,
                "total_score": scores.get("total_score", 0),
                "max_score": scores.get("max_score", 100),
                "market_state": market_state_str,
                "signal_track": 0,
            })
            # V1.0 同源信号
            results_v10.append({
                "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
                "score_ml": score_ml,
                "total_score": scores.get("total_score", 0),
                "max_score": scores.get("max_score", 100),
                "market_state": market_state_str,
            })
            continue  # 已分配到轨道A，跳过B

        # ── 轨道B: V1.1 扩容（ZJTJ-only，不需要MACD） ──
        if (in_zjtj  # ZJTJ单过滤
                and weekly_ok
                and rules_passed >= TB_ENH_MIN
                and (ml_val is not None and ml_val >= TB_ML_MIN)
                and rps_rank <= 5):  # RPS前5板块
            try:
                dk = calculate_kdj(df)
                dz = calculate_zjtj(df)
                scores = compute_total_score(df_macd, dk, dz, rps_rank, ml_score=ml_val)
            except Exception:
                continue
            results_v20.append({
                "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
                "score_ml": score_ml,
                "total_score": scores.get("total_score", 0),
                "max_score": scores.get("max_score", 100),
                "market_state": market_state_str,
                "signal_track": 1,
            })

df_v20 = pd.DataFrame(results_v20)
df_v10 = pd.DataFrame(results_v10)

print(f"\nV2.0 信号总数: {len(df_v20)}")
if not df_v20.empty:
    track_counts = df_v20['signal_track'].value_counts().sort_index()
    for t, c in track_counts.items():
        track_name = {0: "A(V1.0核心)", 1: "B(V1.1扩容)"}
        print(f"  {track_name.get(t, f'Track{t}')}: {c}")
    print(f"  score_ml: mean={df_v20['score_ml'].mean():.1f}, min={df_v20['score_ml'].min():.1f}")
    print(f"  market_state: {df_v20['market_state'].value_counts().to_dict()}")
    df_v20.to_csv("data/output/v2_0_signals_debug.csv", index=False, encoding="utf-8-sig")

print(f"\nV1.0 同源信号: {len(df_v10)}")
if not df_v10.empty:
    print(f"  score_ml: mean={df_v10['score_ml'].mean():.1f}")

# ── V2.0 组合模拟（双轨道差异化执行） ──
print(f"\n开始 V2.0 组合模拟...")
result_v20 = simulate_v20_portfolio(df_v20, stock_daily, trading_dates)

# ── V1.0 对比模拟 ──
print(f"开始 V1.0 对比模拟...")
result_v10 = simulate_pure_portfolio(
    df_v10, stock_daily, trading_dates=trading_dates, dynamic_hold=True,
)

# ── 提取净值 ──
def build_nav_series(daily_nav_list, trading_dates_list):
    raw = [d["nav"] for d in daily_nav_list]
    if not raw:
        return pd.Series(dtype=float)
    return pd.Series(
        [v / 1_000_000 for v in raw],
        index=pd.to_datetime(trading_dates_list),
    )

nav_v20 = build_nav_series(result_v20["daily_nav"], result_v20.get("all_dates", trading_dates))
nav_v10 = build_nav_series(result_v10["daily_nav"], trading_dates)

print(f"\n{'='*80}")
print("V2.2 V1.0+V1.1 双轨道融合 — 回测结果【最终版】")
print(f"{'='*80}")
print(f"{'指标':<20} {'V1.0':<14} {'V2.0(本版)':<14}")
print(f"{'-'*48}")
v10_tr = f"{result_v10.get('total_return', 0):.2f}%"
v20_tr = f"{result_v20.get('total_return', 0):.2f}%"
v10_ar = f"{result_v10.get('ann_return', 0):.2f}%"
v20_ar = f"{result_v20.get('ann_return', 0):.2f}%"
v10_sr = f"{result_v10.get('sharpe', 0):.2f}"
v20_sr = f"{result_v20.get('sharpe', 0):.2f}"
v10_dd = f"{result_v10.get('max_drawdown', 0):.2f}%"
v20_dd = f"{result_v20.get('max_drawdown', 0):.2f}%"
v10_wr = f"{result_v10.get('win_rate', 0):.1f}%"
v20_wr = f"{result_v20.get('win_rate', 0):.1f}%"
v10_pro = f"{result_v10.get('profit_ratio', 0):.2f}"
v20_pro = f"{result_v20.get('profit_ratio', 0):.2f}"
v10_pf = f"{result_v10.get('profit_factor', 0):.2f}"
v20_pf = f"{result_v20.get('profit_factor', 0):.2f}"
v10_tt = f"{result_v10.get('total_trades', 0)}"
v20_tt = f"{result_v20.get('total_trades', 0)}"
print(f"{'总收益率':<20} {v10_tr:<14} {v20_tr:<14}")
print(f"{'年化收益':<20} {v10_ar:<14} {v20_ar:<14}")
print(f"{'夏普比率':<20} {v10_sr:<14} {v20_sr:<14}")
print(f"{'最大回撤':<20} {v10_dd:<14} {v20_dd:<14}")
print(f"{'胜率':<20} {v10_wr:<14} {v20_wr:<14}")
print(f"{'盈亏比':<20} {v10_pro:<14} {v20_pro:<14}")
print(f"{'利润因子':<20} {v10_pf:<14} {v20_pf:<14}")
print(f"{'交易次数':<20} {v10_tt:<14} {v20_tt:<14}")

# 各track统计
track_stats = result_v20.get("track_stats", {})
if track_stats:
    print(f"\nV2.0 各轨道统计:")
    track_names = {0: "A(V1.0核心)", 1: "B(V1.1扩容)"}
    for tid, stats in sorted(track_stats.items()):
        print(f"  {track_names.get(tid, f'Track{tid}')}: "
              f"交易{stats['count']}次, "
              f"胜率{stats['win_rate']:.1f}%, "
              f"平均回报{stats['avg_return']:.2f}%")

exit_reasons = result_v20.get("exit_reasons", {})
if exit_reasons:
    print(f"\n退出原因分布:")
    for reason, cnt in sorted(exit_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {cnt}")

# ── 绘图 ──
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

# 基准指数归一化
bench_norm = {}
for name, df in benchmark_data.items():
    dates = pd.to_datetime(df["date"]).dt.tz_localize(None)
    prices = df["close"].values.astype(float)
    if len(prices) == 0:
        continue
    base_price = prices[0]
    norm_prices = prices / base_price
    ts = pd.Series(norm_prices, index=dates)
    bench_norm[name] = ts

from matplotlib.dates import DateFormatter, MonthLocator

fig, axes = plt.subplots(3, 1, figsize=(16, 14), sharex=True)
fig.suptitle("66大顺 V2.2 V1.0+V1.1 双轨道融合回测曲线【最终版】", fontsize=18, fontweight="bold")

# 1) 累计收益率
ax1 = axes[0]
if not nav_v20.empty:
    ax1.plot(nav_v20.index, (nav_v20.values - 1) * 100,
             label=f"66大顺 V2.0 ({v20_ar}年化 / {v20_dd}回撤)",
             color="#0066CC", linewidth=2.5)
if not nav_v10.empty:
    ax1.plot(nav_v10.index, (nav_v10.values - 1) * 100,
             label=f"66大顺 V1.0 ({v10_ar}年化 / {v10_dd}回撤)",
             color="#FF6B35", linewidth=2, alpha=0.8)
for name, ts in bench_norm.items():
    common = (nav_v20.index if not nav_v20.empty else nav_v10.index).tz_localize(None).intersection(ts.index)
    if len(common) > 0:
        base_val = ts.loc[common[0]]
        ret_vals = (ts.loc[common] / base_val - 1) * 100
        ax1.plot(common, ret_vals.values, label=name, linewidth=1.5, alpha=0.7)
ax1.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
ax1.set_ylabel("累计收益率 (%)")
ax1.legend(loc="upper left", fontsize=11)
ax1.grid(True, alpha=0.3)
ax1.set_title("累计收益率曲线对比 (V2.0 vs V1.0)", fontsize=14, fontweight="bold")

# 2) 回撤曲线
ax2 = axes[1]
if not nav_v20.empty:
    peak = nav_v20.cummax()
    dd = (nav_v20 - peak) / peak * 100
    ax2.fill_between(nav_v20.index, dd.values, 0, color="#0066CC", alpha=0.4,
                     label=f"V2.0 最大回撤 {v20_dd}")
if not nav_v10.empty:
    peak10 = nav_v10.cummax()
    dd10 = (nav_v10 - peak10) / peak10 * 100
    ax2.fill_between(nav_v10.index, dd10.values, 0, color="#FF6B35", alpha=0.3,
                     label=f"V1.0 最大回撤 {v10_dd}")
ax2.set_ylabel("回撤 (%)")
ax2.legend(loc="lower left", fontsize=11)
ax2.grid(True, alpha=0.3)
ax2.set_title("回撤曲线对比", fontsize=14, fontweight="bold")

# 3) 滚动夏普
ax3 = axes[2]
window = 63
for nav_s, name, clr in [(nav_v20, "V2.0", "#0066CC"), (nav_v10, "V1.0", "#FF6B35")]:
    if nav_s.empty or len(nav_s) < window:
        continue
    daily_ret = nav_s.pct_change().dropna()
    rolling_sharpe = daily_ret.rolling(window).apply(
        lambda x: (x.mean() - 0.02/250) / x.std() * np.sqrt(250) if x.std() > 0 else 0
    )
    ax3.plot(rolling_sharpe.index, rolling_sharpe.values, color=clr, linewidth=1.5,
             label=f"{name} 滚动夏普", alpha=0.8)

ax3.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
ax3.set_ylabel("夏普比率")
ax3.legend(loc="upper left", fontsize=11)
ax3.grid(True, alpha=0.3)
ax3.set_title("滚动夏普比率 (63个交易日窗口)", fontsize=14, fontweight="bold")

ax3.xaxis.set_major_locator(MonthLocator(interval=2))
ax3.xaxis.set_minor_locator(MonthLocator())
ax3.xaxis.set_major_formatter(DateFormatter('%Y-%m'))
plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=9)
plt.tight_layout()
fig.subplots_adjust(bottom=0.08)
output_path = os.path.join("data", "output", "66dashun_v2_2_final_curve.png")
plt.savefig(output_path, dpi=150, bbox_inches="tight")
print(f"\n图表已保存: {output_path}")
plt.close()

# ── 最终总结 ──
print(f"\n{'='*80}")
print("66大顺 V2.2 V1.0+V1.1 双轨道融合 — 总结【最终版】")
print(f"{'='*80}")
print(f"  V1.0: {len(df_v10)}信号, {v10_ar}年化, {v10_dd}回撤, 夏普{v10_sr}")
print(f"  V2.0: {len(df_v20)}信号, {v20_ar}年化, {v20_dd}回撤, 夏普{v20_sr}")
ann = result_v20.get("ann_return", 0)
dd = result_v20.get("max_drawdown", 0)
if ann >= 25 and dd <= 10:
    target_hit = "  【目标达成】年化≥25% 且 回撤≤10%  ✓"
elif ann >= 18 and dd <= 10:
    target_hit = "  【接近目标】年化≥18%，回撤可控"
elif ann >= 15 and dd <= 10:
    target_hit = "  【改良显著】超越V1.0但需进一步优化"
else:
    target_hit = "  【需要优化】调整参数或增加扩容力度"
print(target_hit)
print(f"{'='*80}")
