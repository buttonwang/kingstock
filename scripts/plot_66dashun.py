"""绘制66大顺收益率曲线、夏普比率、回撤率图表"""
import os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)  # 确保工作目录正确

from src.data_fetcher import DataFetcher
from src.market_state import get_market_state, is_tradeable, get_ml_min_threshold
from config.settings import RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT, ENHANCED_RULES_MIN
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj
from src.portfolio_manager import simulate_pure_portfolio

start_date, end_date = "20230601", "20260603"

# ── 加载数据 ──
fetcher = DataFetcher()
fmt_start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
fmt_end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

sec_df = fetcher._sql_to_df("SELECT date, sector_name, close FROM sector_daily WHERE date>=? AND date<=? ORDER BY date", params=(fmt_start, fmt_end))
sector_daily = {}
for _, row in sec_df.iterrows():
    sector_daily.setdefault(row["sector_name"], []).append({"date": row["date"], "close": row["close"]})
sector_daily = {k: pd.DataFrame(v) for k, v in sector_daily.items()}

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

# 获取沪深300和中证800指数日线
index_codes = {"000300.SH": "沪深300", "000906.SH": "中证800"}
benchmark_data = {}
for idx_code, idx_name in index_codes.items():
    df = fetcher._sql_to_df(
        "SELECT date, close FROM stock_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
        params=(idx_code, fmt_start, fmt_end),
    )
    if not df.empty:
        benchmark_data[idx_name] = df

fetcher.close()

# 交易日列表
all_dates = set()
for df in sector_daily.values():
    all_dates.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates
                       if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))

# ── 信号生成（66大顺完整流程） ──
results = []
from src.scoring import compute_total_score
from src.indicators.macd import calculate_macd
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.ml import ml_scorer
from src.market_state import get_market_state, is_tradeable, get_ml_min_threshold
from config.settings import (
    RPS_TOP_N_STRICT, ML_SCORE_MIN_THRESHOLD,
    SCORE_THRESHOLD_STRONG, SCORE_THRESHOLD_CHOPPY,
    ENHANCED_RULES_MIN, WEAK_MARKET_MAX_SIGNALS,
)

def check_weekly_trend(df):
    if df is None or len(df) < 60:
        return True
    df_w = df.copy()
    df_w["date"] = pd.to_datetime(df_w["date"])
    df_w = df_w.set_index("date").resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    if len(df_w) < 12:
        return True
    dm = calculate_macd(df_w)
    if dm is None or "macd" not in dm.columns or "dif" not in dm.columns or "dea" not in dm.columns:
        return True
    return dm["macd"].values[-1] > 0 and dm["dif"].values[-1] > dm["dea"].values[-1]

ml_avail = ml_scorer.is_available()
total_dates = len(trading_dates)

for di, date_str in enumerate(trading_dates):
    if di % max(1, total_dates // 20) == 0:
        print(f"回测 {di}/{total_dates} ({100*di//total_dates}%)")
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
    kdj_codes = filter_by_kdj(stock_dict)
    core_codes = macd_codes & zjtj_codes
    enhanced_codes = set()
    for code in core_codes:
        sub_df = stock_dict.get(code)
        if sub_df is not None and check_weekly_trend(sub_df):
            enhanced_codes.add(code)
    core_codes = enhanced_codes
    if not core_codes:
        continue
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
    for code in core_codes:
        df = stock_dict[code]
        sector = code_to_sector[code]
        rps_rank = rps_rank_map.get(sector, RPS_TOP_N)
        ml_val = None
        if ml_avail:
            try:
                ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
            except Exception:
                pass
        ml_threshold = get_ml_min_threshold(market_state)
        if ml_val is not None and ml_val < ml_threshold:
            continue
        try:
            dm = calculate_macd(df)
            dk = calculate_kdj(df)
            dz = calculate_zjtj(df)
            scores = compute_total_score(dm, dk, dz, rps_rank, ml_score=ml_val)
            enh_info = check_all_enhanced_rules(df)
            if enh_info["rules_passed"] < ENHANCED_RULES_MIN:
                continue
        except Exception:
            continue
        results.append({
            "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
            "score_ml": ml_val or 0, "market_state": market_state_str,
        })

df_result = pd.DataFrame(results)
print(f"信号总数: {len(df_result)}")

# ── 组合模拟（66大顺） ──
result = simulate_pure_portfolio(
    df_result[["date", "code", "name", "score_ml"]],
    stock_daily, trading_dates=trading_dates,
    dynamic_hold=True,
)

# ── 提取净值曲线 ──
nav_series = result["daily_nav"]
print(f"净值曲线长度: {len(nav_series)}")
print(f"最终净值: {nav_series.iloc[-1]:.4f}, 收益率: {result['total_return']:.2f}%")

# ── 基准指数归一化 ──
bench_norm = {}
for name, df in benchmark_data.items():
    dates = pd.to_datetime(df["date"])
    prices = df["close"].values.astype(float)
    base_price = prices[0]
    norm = prices / base_price
    ts = pd.Series(norm, index=dates)
    bench_norm[name] = ts

# ── 将净值曲线对齐到相同日期索引 ──
nav_idx = pd.to_datetime(nav_series.index if hasattr(nav_series.index, 'dtype') else nav_series.index)

# ── 绘图 ──
fig, axes = plt.subplots(3, 1, figsize=(16, 14), sharex=True)
fig.suptitle("66大顺 V1.0 纯信号策略回测曲线", fontsize=18, fontweight="bold")

# 1) 累计收益率曲线
ax1 = axes[0]
ax1.plot(nav_series.index, (nav_series.values - 1) * 100, label="66大顺 V1.0", color="#FF6B35", linewidth=2)
for name, ts in bench_norm.items():
    # 对齐到净值曲线的日期
    common_dates = [d for d in nav_series.index if d in ts.index]
    if common_dates:
        vals = [ts[d] for d in common_dates]
        base = ts[common_dates[0]]
        ret = [(v / base - 1) * 100 for v in vals]
        ax1.plot(common_dates, ret, label=name, linewidth=1.5, alpha=0.7)
ax1.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
ax1.set_ylabel("累计收益率 (%)")
ax1.legend(loc="upper left", fontsize=11)
ax1.grid(True, alpha=0.3)
ax1.set_title("累计收益率曲线", fontsize=14, fontweight="bold")

# 2) 回撤曲线
ax2 = axes[1]
peak = nav_series.cummax()
drawdown = (nav_series - peak) / peak * 100
ax2.fill_between(nav_series.index, drawdown.values, 0, color="#FF4444", alpha=0.5, label=f"最大回撤 {result['max_drawdown']:.2f}%")
ax2.set_ylabel("回撤 (%)")
ax2.legend(loc="lower left", fontsize=11)
ax2.grid(True, alpha=0.3)
ax2.set_title("回撤曲线", fontsize=14, fontweight="bold")

# 3) 滚动夏普比率
ax3 = axes[2]
daily_ret = nav_series.pct_change().dropna()
window = 63  # 约3个月
rolling_sharpe = daily_ret.rolling(window).apply(
    lambda x: (x.mean() - 0.02/250) / x.std() * np.sqrt(250) if x.std() > 0 else 0
)
ax3.plot(rolling_sharpe.index, rolling_sharpe.values, color="#4A90D9", linewidth=1.5, label=f"63日滚动夏普")
ax3.axhline(y=result["sharpe"], color="#4A90D9", linewidth=1, linestyle="--", alpha=0.7, label=f"全周期夏普 {result['sharpe']:.2f}")
ax3.fill_between(rolling_sharpe.index, 0, rolling_sharpe.values, where=(rolling_sharpe.values >= 0), color="#4A90D9", alpha=0.3)
ax3.fill_between(rolling_sharpe.index, rolling_sharpe.values, 0, where=(rolling_sharpe.values < 0), color="#FF4444", alpha=0.3)
ax3.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
ax3.set_ylabel("夏普比率")
ax3.legend(loc="upper left", fontsize=11)
ax3.grid(True, alpha=0.3)
ax3.set_title("滚动夏普比率 (63个交易日窗口)", fontsize=14, fontweight="bold")

plt.tight_layout()
output_path = os.path.join("data", "output", "66dashun_curve.png")
plt.savefig(output_path, dpi=150, bbox_inches="tight")
print(f"图表已保存: {output_path}")
plt.close()
