"""66大顺 V1.5 回测脚本

V1.0 + 放宽增强规则（B方案测试）:
- 信号: MACD ∩ ZJTJ（严格MACD，同V1.0）
- 增强规则: ≥0（去掉约束，测试信号量能否增加）
- 执行: V1.0风格（纯持有到期，动态持有期，市场状态门控）
- 目标: 验证去掉增强规则后能否在保持质量的同时增加信号量

Version: 1.5
Date: 2026-06-06
对比基准: V1.0 (16.28%年化, 回撤-5.98%)
         V1.4 (12.44%年化, 回撤-8.53%)
"""

import os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
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
from src.portfolio_manager import _get_price_at_date

start_date, end_date = "20230601", "20260603"

# V1.5 配置（唯一变化: enhanced_rules_min=0）
V1_5_CONFIG = {
    'track': {
        'ml_min': 10,
        'enhanced_rules_min': 0,  # B方案：去掉增强规则约束
        'hold_days': {13: 7, 11: 5, 10: 3},
        'max_position_pct': 0.05,
    },
    'market_mult': {0: {'strong': 1.0, 'choppy': 0.5, 'weak_reduced': 0.0, 'weak': 0.0}},
    'weak_market_ml_threshold': 14,
}

print("=" * 80)
print("66大顺 V1.5 回测开始 — V1.0 + 放宽增强规则")
print("=" * 80)
print(f"\n优化配置:")
print(f"  信号: MACD∩ZJTJ（严格MACD，同V1.0）")
print(f"  增强规则: 去掉约束（enhanced_rules_min=0）")
print(f"  ML≥10, 动态持有期7/5/3天, 纯持有到期")
print(f"  市场门控: 强市全开 | 震荡半仓 | 弱市不交易")
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

# ── 信号生成 ──
from src.scoring import compute_total_score
from src.indicators.macd import calculate_macd
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.ml import ml_scorer

ml_avail = ml_scorer.is_available()
total_dates = len(trading_dates)

print(f"\n开始生成信号（V1.5 MACD∩ZJTJ + 放宽增强规则）...")
print(f"总交易日: {total_dates}")

all_signals = []

for di, date_str in enumerate(trading_dates):
    if di % max(1, total_dates // 20) == 0:
        print(f"  进度 {di}/{total_dates} ({100*di//total_dates}%)")

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

    # V1.0严格信号：MACD ∩ ZJTJ
    macd_codes = filter_by_macd(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)
    core_codes = macd_codes & zjtj_codes

    if not core_codes:
        continue

    # 市场状态
    past_returns = []
    for code, df in stock_dict.items():
        closes = df["close"].values
        if len(closes) >= 11:
            past_returns.append((closes[-1] / closes[-11] - 1) * 100)
    market_10d_past = np.mean(past_returns) if past_returns else 0
    market_state_raw = get_market_state(market_10d_past)
    if not is_tradeable(market_state_raw):
        market_state = "weak_reduced"
    else:
        market_state = market_state_raw

    cfg = V1_5_CONFIG['track']
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

        # ML门槛（V1.0标准）
        if V1_5_CONFIG.get('weak_market_relax', False) and not is_tradeable(market_state_raw):
            ml_threshold = V1_5_CONFIG.get('weak_market_ml_threshold', 14)
        else:
            ml_threshold = get_ml_min_threshold(market_state_raw)
        ml_threshold = max(ml_threshold, cfg['ml_min'])
        if ml_val is not None and ml_val < ml_threshold:
            continue

        try:
            dm = calculate_macd(df)
            dk = calculate_kdj(df)
            dz = calculate_zjtj(df)
            scores = compute_total_score(dm, dk, dz, rps_rank, ml_score=ml_val)

            # 增强规则：V1.5去掉了约束（min=0），但仍然计算用于统计
            enh_info = check_all_enhanced_rules(df)
            if enh_info["rules_passed"] < cfg['enhanced_rules_min']:
                continue
        except Exception:
            continue

        # 弱势市场ML≥14
        if market_state == "weak_reduced" and (ml_val is None or ml_val < V1_5_CONFIG['weak_market_ml_threshold']):
            continue

        all_signals.append({
            "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
            "score_ml": scores.get("score_ml", 0),
            "total_score": scores.get("total_score", 0),
            "max_score": scores.get("max_score", 100),
            "market_state": market_state,
            "enhanced_rules": enh_info.get("rules_passed", 0),
        })

df_signals = pd.DataFrame(all_signals)
print(f"\n信号总数: {len(df_signals)}")
if not df_signals.empty:
    print(f"  score_ml: mean={df_signals['score_ml'].mean():.1f}, min={df_signals['score_ml'].min():.1f}")
    # 统计增强规则分布
    if 'enhanced_rules' in df_signals.columns:
        print(f"  增强规则分布: {df_signals['enhanced_rules'].value_counts().sort_index().to_dict()}")
    df_signals.to_csv("data/output/v1_5_signals_debug.csv", index=False, encoding="utf-8-sig")

# ────────────────────────────────────────────────────────────
# V1.5 组合模拟（纯持有到期，同V1.0）
# ────────────────────────────────────────────────────────────
print(f"\n开始组合模拟（V1.5 Pure Hold模式）...")

def simulate_v15_portfolio(signals_df, stock_daily, trading_dates, initial_capital=1_000_000):
    if signals_df.empty:
        return {"total_trades": 0, "final_value": initial_capital, "total_return": 0}

    signals_df = signals_df.copy()
    signals_df["date"] = signals_df["date"].astype(str)
    signals = signals_df.to_dict("records")
    signal_lookup = defaultdict(list)
    for sig in signals:
        signal_lookup[sig["date"]].append(sig)

    all_dates = sorted(trading_dates) if trading_dates else sorted(signal_lookup.keys())
    if not all_dates:
        return {"total_trades": 0}

    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    active = {}
    closed_trades = []
    daily_nav = []
    capital = initial_capital

    for date_str in all_dates:
        today_signals = signal_lookup.get(date_str, [])
        today_idx = date_to_idx.get(date_str)

        # 检查持仓：到期卖出
        to_close = []
        for code, pos in list(active.items()):
            stock_df = stock_daily.get(code)
            if stock_df is None or stock_df.empty:
                exit_price = pos["entry_price"]
                to_close.append((code, pos, exit_price, date_str, "NO_DATA"))
                continue
            current_price = _get_price_at_date(stock_df, date_str)
            if current_price <= 0:
                current_price = pos["entry_price"]

            if current_price > pos.get("peak_price", pos["entry_price"]):
                pos["peak_price"] = current_price

            ret = (current_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0

            exit_idx = pos.get("exit_idx")
            if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                to_close.append((code, pos, current_price, date_str, "PURE_HOLD"))

        for code, pos, exit_price, exit_date, reason in to_close:
            ret = (exit_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0
            trade_pnl = pos["position_size"] * ret / 100
            capital += pos["position_size"] + trade_pnl
            closed_trades.append({
                "code": code, "entry_date": pos["entry_date"],
                "exit_date": exit_date, "entry_price": pos["entry_price"],
                "exit_price": exit_price, "position_size": pos["position_size"],
                "return_pct": round(ret, 2), "pnl": round(trade_pnl, 2),
                "exit_reason": reason,
                "entry_score_ml": pos.get("entry_score_ml", 0),
                "held_days": pos.get("held_days", 0),
            })
            del active[code]

        # 当日新信号买入
        if today_signals:
            # 按ML评分排序
            today_sorted = sorted(today_signals, key=lambda x: x.get("score_ml", 0), reverse=True)

            for sig in today_sorted:
                code = sig["code"]
                if code in active:
                    continue

                ms_sig = sig.get("market_state", "strong")
                mult = V1_5_CONFIG['market_mult'][0].get(ms_sig, 0)
                if mult <= 0:
                    continue

                max_pos_pct = V1_5_CONFIG['track']['max_position_pct']
                pos_size = capital * max_pos_pct * mult
                pos_size = min(pos_size, capital * 0.95)
                if pos_size <= 0:
                    continue

                stock_df = stock_daily.get(code)
                if stock_df is None:
                    continue
                entry_price = _get_price_at_date(stock_df, date_str)
                if entry_price <= 0:
                    continue

                capital -= pos_size

                ml = sig.get("score_ml", 10)
                hd_map = V1_5_CONFIG['track']['hold_days']
                hold_days = 3
                for threshold, days in sorted(hd_map.items(), reverse=True):
                    if ml >= threshold:
                        hold_days = days
                        break

                exit_idx = today_idx + hold_days if today_idx is not None else None

                active[code] = {
                    "entry_date": date_str,
                    "entry_price": entry_price,
                    "position_size": pos_size,
                    "entry_score_ml": ml,
                    "entry_total_score": sig.get("total_score", 0),
                    "exit_idx": exit_idx,
                    "held_days": hold_days,
                    "peak_price": entry_price,
                }

        # 每日净值
        pos_values = sum(p["position_size"] for p in active.values())
        daily_nav.append({"date": date_str, "nav": round(capital + pos_values, 2)})

    # 强制平仓
    for code, pos in list(active.items()):
        stock_df = stock_daily.get(code)
        if stock_df is not None and not stock_df.empty:
            exit_price = _get_price_at_date(stock_df, daily_nav[-1]["date"]) if daily_nav else pos["entry_price"]
        else:
            exit_price = pos["entry_price"]
        if exit_price <= 0:
            exit_price = pos["entry_price"]
        ret = (exit_price / pos["entry_price"] - 1) * 100
        trade_pnl = pos["position_size"] * ret / 100
        capital += pos["position_size"] + trade_pnl
        closed_trades.append({
            "code": code, "entry_date": pos["entry_date"],
            "exit_date": daily_nav[-1]["date"] if daily_nav else "UNKNOWN",
            "entry_price": pos["entry_price"], "exit_price": exit_price,
            "position_size": pos["position_size"], "return_pct": round(ret, 2),
            "pnl": round(trade_pnl, 2), "exit_reason": "FORCED_CLOSE",
            "entry_score_ml": pos.get("entry_score_ml", 0),
            "held_days": pos.get("held_days", 0),
        })

    final_value = capital
    total_return = (final_value / initial_capital - 1) * 100

    nav_series = pd.Series([d["nav"] for d in daily_nav])
    n_days = len(daily_nav)
    ann_return = (final_value / initial_capital) ** (250 / max(n_days, 1)) - 1 if n_days > 0 else 0

    peak = nav_series.cummax()
    drawdowns = (nav_series - peak) / peak * 100
    max_dd = drawdowns.min() if len(drawdowns) > 0 else 0

    daily_returns = nav_series.pct_change().dropna()
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() - 0.02 / 250) / daily_returns.std() * np.sqrt(250)
    else:
        sharpe = 0

    closed_df = pd.DataFrame(closed_trades)
    if not closed_df.empty:
        wins = closed_df[closed_df["return_pct"] > 0]
        losses = closed_df[closed_df["return_pct"] <= 0]
        win_rate = len(wins) / len(closed_df) * 100 if len(closed_df) > 0 else 0
        avg_win = wins["return_pct"].mean() if len(wins) > 0 else 0
        avg_loss = abs(losses["return_pct"].mean()) if len(losses) > 0 else 0
        profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses["pnl"].sum() != 0 else float("inf")
    else:
        win_rate = profit_ratio = profit_factor = 0

    exit_reasons = {}
    for trade in closed_trades:
        r = trade["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    return {
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "ann_return": round(ann_return * 100, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "win_rate": round(win_rate, 1),
        "profit_ratio": round(profit_ratio, 2),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(closed_trades),
        "exit_reasons": exit_reasons,
        "daily_nav": daily_nav,
        "closed_trades": closed_trades,
    }

result = simulate_v15_portfolio(df_signals, stock_daily, trading_dates)

# ── 净值曲线 ──
daily_nav_list = result["daily_nav"]
initial_capital = 1_000_000.0
raw_nav = [d["nav"] for d in daily_nav_list]
nav_series = pd.Series(
    [v / initial_capital for v in raw_nav],
    index=pd.to_datetime(trading_dates),
)
print(f"\n净值曲线长度: {len(nav_series)}")
print(f"最终净值: {nav_series.iloc[-1]:.4f}, 收益率: {result['total_return']:.2f}%")
print(f"夏普比率: {result['sharpe']:.2f}, 最大回撤: {result['max_drawdown']:.2f}%")
print(f"胜率: {result['win_rate']:.1f}%, 交易次数: {result['total_trades']}")

# ── 基准指数归一化 ──
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

# ── 绘图 ──
from matplotlib.dates import DateFormatter, MonthLocator

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

fig, axes = plt.subplots(3, 1, figsize=(16, 14), sharex=True)
fig.suptitle("66大顺 V1.5 放宽增强规则回测曲线", fontsize=18, fontweight="bold")

ax1 = axes[0]
ax1.plot(nav_series.index, (nav_series.values - 1) * 100, label="66大顺 V1.5", color="#0066CC", linewidth=2.5)
for name, ts in bench_norm.items():
    nav_idx = nav_series.index.tz_localize(None) if nav_series.index.tz else nav_series.index
    common = nav_idx.intersection(ts.index)
    if len(common) > 0:
        base_val = ts.loc[common[0]]
        ret_vals = (ts.loc[common] / base_val - 1) * 100
        ax1.plot(common, ret_vals.values, label=name, linewidth=1.5, alpha=0.8)
ax1.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
ax1.set_ylabel("累计收益率 (%)")
ax1.legend(loc="upper left", fontsize=11)
ax1.grid(True, alpha=0.3)
ax1.set_title("累计收益率曲线 (V1.5 MACD∩ZJTJ + 放宽增强规则)", fontsize=14, fontweight="bold")

ax2 = axes[1]
peak = nav_series.cummax()
drawdown = (nav_series - peak) / peak * 100
ax2.fill_between(nav_series.index, drawdown.values, 0, color="#FF4444", alpha=0.5,
                 label=f"最大回撤 {result['max_drawdown']:.2f}%")
ax2.set_ylabel("回撤 (%)")
ax2.legend(loc="lower left", fontsize=11)
ax2.grid(True, alpha=0.3)
ax2.set_title("回撤曲线", fontsize=14, fontweight="bold")

ax3 = axes[2]
daily_ret = nav_series.pct_change().dropna()
window = 63
rolling_sharpe = daily_ret.rolling(window).apply(
    lambda x: (x.mean() - 0.02/250) / x.std() * np.sqrt(250) if x.std() > 0 else 0
)
ax3.plot(rolling_sharpe.index, rolling_sharpe.values, color="#4A90D9", linewidth=1.5, label="63日滚动夏普")
ax3.axhline(y=result["sharpe"], color="#4A90D9", linewidth=1, linestyle="--", alpha=0.7,
            label=f"全周期夏普 {result['sharpe']:.2f}")
ax3.fill_between(rolling_sharpe.index, 0, rolling_sharpe.values, where=(rolling_sharpe.values >= 0),
                 color="#4A90D9", alpha=0.3)
ax3.fill_between(rolling_sharpe.index, rolling_sharpe.values, 0, where=(rolling_sharpe.values < 0),
                 color="#FF4444", alpha=0.3)
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
output_path = os.path.join("data", "output", "66dashun_v1_5_curve.png")
plt.savefig(output_path, dpi=150, bbox_inches="tight")
print(f"\n图表已保存: {output_path}")
plt.close()

# ── 对比总结 ──
print("\n" + "=" * 80)
print("66大顺 V1.5 回测结果总结 — V1.0 + 放宽增强规则")
print("=" * 80)
print(f"{'指标':<20} {'V1.0':<12} {'V1.4':<12} {'V1.5(本版)':<12}")
print("-" * 56)
tr = f"{result.get('total_return', 0):.2f}%"
ar = f"{result.get('ann_return', 0):.2f}%"
sr = f"{result.get('sharpe', 0):.2f}"
dd = f"{result.get('max_drawdown', 0):.2f}%"
wr = f"{result.get('win_rate', 0):.1f}%"
pro = f"{result.get('profit_ratio', 0):.2f}"
pf = f"{result.get('profit_factor', 0):.2f}"
tt = f"{result.get('total_trades', 0)}"
sign = f"{len(df_signals)}"
print(f"{'总收益率':<20} {'46.90%':<12} {'40.57%':<12} {tr:<12}")
print(f"{'年化收益':<20} {'16.28%':<12} {'12.44%':<12} {ar:<12}")
print(f"{'夏普比率':<20} {'3.82':<12} {'1.32':<12} {sr:<12}")
print(f"{'最大回撤':<20} {'-5.98%':<12} {'-8.53%':<12} {dd:<12}")
print(f"{'胜率':<20} {'~60%':<12} {'48.4%':<12} {wr:<12}")
print(f"{'盈亏比':<20} {'2.1+':<12} {'1.69':<12} {pro:<12}")
print(f"{'利润因子':<20} {'N/A':<12} {'1.68':<12} {pf:<12}")
print(f"{'交易次数':<20} {'541':<12} {'699':<12} {tt:<12}")
print(f"{'信号总数':<20} {'551':<12} {'783':<12} {sign:<12}")

print(f"\n退出原因: {result.get('exit_reasons', {})}")
print("\n对比分析:")
print(f"  ✅ V1.0 (MACD∩ZJTJ, 增强≥1): {551}信号, 16.28%年化, -5.98%回撤")
print(f"  ✅ V1.5 (MACD∩ZJTJ, 增强≥0): {sign}信号, {ar}年化, {dd}回撤")
print("=" * 80)
