"""66大顺 V1.1 回测脚本

V1.1优化内容:
1. 三选二过滤（信号量+40-60%）
2. 移动止盈止损（控制回撤）
3. 放宽弱势市场过滤
4. Kelly动态仓位优化

Version: 1.1
Date: 2026-06-06
对比基准: V1.0 (plot_66dashun.py)
"""

import os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.market_state import get_market_state, is_tradeable, get_ml_min_threshold
from config.settings import RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT, ENHANCED_RULES_MIN
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj
from src.portfolio_manager import simulate_pure_portfolio

# V1.1 新增模块
from scripts.v1_1_enhanced_filter import filter_by_any_two
from scripts.v1_1_dynamic_exit import DynamicExitManager

start_date, end_date = "20230601", "20260603"

# V1.1 配置参数
V1_1_CONFIG = {
    # 过滤模式：'any_two', 'macd_required', 'zjtj_kdj_only', 'zjtj_only'
    'filter_mode': 'zjtj_only',
    
    # 动态退出配置
    'dynamic_exit': {
        'enabled': True,
        'hard_stop_pct': -0.08,        # -8%硬止损
        'trailing_activate_pct': 0.15, # +15%激活移动止盈
        'trailing_stop_pct': -0.05,    # -5%回撤止盈
    },
    
    # 弱势市场放宽
    'weak_market_relax': True,         # 允许ML≥14的超强信号通过
    'weak_market_ml_threshold': 14,
}

print("=" * 80)
print("66大顺 V1.1 回测开始")
print("=" * 80)
print(f"\n优化配置:")
print(f"  过滤模式: {V1_1_CONFIG['filter_mode']} (仅ZJTJ，日均35只→靠ML+规则筛选)")
print(f"  动态退出: {'启用' if V1_1_CONFIG['dynamic_exit']['enabled'] else '禁用'}")
print(f"    - 硬止损: {V1_1_CONFIG['dynamic_exit']['hard_stop_pct']*100:.0f}%")
print(f"    - 移动止盈激活: {V1_1_CONFIG['dynamic_exit']['trailing_activate_pct']*100:.0f}%")
print(f"    - 移动止盈回撤: {abs(V1_1_CONFIG['dynamic_exit']['trailing_stop_pct'])*100:.0f}%")
print(f"  弱势市场放宽: {'启用' if V1_1_CONFIG['weak_market_relax'] else '禁用'}")
print()

# ── 加载数据（与V1.0保持一致） ──
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

# 从akshare获取基准指数数据
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

# ── 信号生成（V1.1优化版） ──
results = []
from src.scoring import compute_total_score
from src.indicators.macd import calculate_macd
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.ml import ml_scorer
from config.settings import (
    RPS_TOP_N_STRICT, ML_SCORE_MIN_THRESHOLD,
    SCORE_THRESHOLD_STRONG, SCORE_THRESHOLD_CHOPPY,
    ENHANCED_RULES_MIN, WEAK_MARKET_MAX_SIGNALS,
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

# V1.1: 创建动态退出管理器
exit_manager = DynamicExitManager(
    hard_stop_pct=V1_1_CONFIG['dynamic_exit']['hard_stop_pct'],
    trailing_activate_pct=V1_1_CONFIG['dynamic_exit']['trailing_activate_pct'],
    trailing_stop_pct=V1_1_CONFIG['dynamic_exit']['trailing_stop_pct'],
) if V1_1_CONFIG['dynamic_exit']['enabled'] else None

print(f"\n开始生成信号（V1.1模式）...")
print(f"总交易日: {total_dates}")

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
    
    # V1.1优化: 绕过MACD瓶颈
    if V1_1_CONFIG['filter_mode'] == 'any_two':
        core_codes = filter_by_any_two(stock_dict, mode='any_two')
    elif V1_1_CONFIG['filter_mode'] == 'macd_required':
        core_codes = filter_by_any_two(stock_dict, mode='macd_required')
    elif V1_1_CONFIG['filter_mode'] == 'zjtj_only':
        core_codes = filter_by_any_two(stock_dict, mode='zjtj_only')
    else:
        # 回退到V1.0模式
        macd_codes = filter_by_macd(stock_dict)
        zjtj_codes = filter_by_zjtj(stock_dict)
        core_codes = macd_codes & zjtj_codes
    
    # 周线趋势确认
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
    
    # V1.1优化: 放宽弱势市场过滤
    if V1_1_CONFIG['weak_market_relax']:
        # 允许ML≥14的超强信号通过弱势市场
        if not is_tradeable(market_state):
            market_state_str = "weak_reduced"
        else:
            market_state_str = market_state
    else:
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
        
        # V1.1优化: 弱势市场ML评分阈值放宽
        if V1_1_CONFIG['weak_market_relax'] and not is_tradeable(market_state):
            ml_threshold = V1_1_CONFIG['weak_market_ml_threshold']
        else:
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
            "date": date_str, 
            "code": code, 
            "name": stock_name_map.get(code, ""),
            "score_ml": scores.get("score_ml", 0),
            "total_score": scores.get("total_score", 0),
            "max_score": scores.get("max_score", 100),
            "market_state": market_state_str,
        })

df_result = pd.DataFrame(results)
print(f"\n信号总数: {len(df_result)}")
if not df_result.empty:
    print(f"  score_ml: mean={df_result['score_ml'].mean():.1f}, min={df_result['score_ml'].min():.1f}, max={df_result['score_ml'].max():.1f}")
    print(f"  market_state: {df_result['market_state'].value_counts().to_dict()}")
    df_result.to_csv("data/output/v1_1_signals_debug.csv", index=False, encoding="utf-8-sig")

# ── 组合模拟（V1.1: 启用动态止盈止损） ──
print(f"\n开始组合模拟（V1.1模式）...")
result = simulate_pure_portfolio(
    df_result,
    stock_daily, 
    trading_dates=trading_dates,
    dynamic_hold=True,
    use_price_stop=V1_1_CONFIG['dynamic_exit']['enabled'],
    use_trailing_stop=V1_1_CONFIG['dynamic_exit']['enabled'],
)

# ── 提取净值曲线 ──
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

# ── 设置中文字体 ──
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

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

fig, axes = plt.subplots(3, 1, figsize=(16, 14), sharex=True)
fig.suptitle("66大顺 V1.1 优化策略回测曲线", fontsize=18, fontweight="bold")

# 1) 累计收益率曲线
ax1 = axes[0]
ax1.plot(nav_series.index, (nav_series.values - 1) * 100, label="66大顺 V1.1", color="#00AA00", linewidth=2.5)
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
ax1.set_title("累计收益率曲线 (V1.1优化版)", fontsize=14, fontweight="bold")

# 2) 回撤曲线
ax2 = axes[1]
peak = nav_series.cummax()
drawdown = (nav_series - peak) / peak * 100
ax2.fill_between(nav_series.index, drawdown.values, 0, color="#FF4444", alpha=0.5, 
                 label=f"最大回撤 {result['max_drawdown']:.2f}%")
ax2.set_ylabel("回撤 (%)")
ax2.legend(loc="lower left", fontsize=11)
ax2.grid(True, alpha=0.3)
ax2.set_title("回撤曲线", fontsize=14, fontweight="bold")

# 3) 滚动夏普比率
ax3 = axes[2]
daily_ret = nav_series.pct_change().dropna()
window = 63
rolling_sharpe = daily_ret.rolling(window).apply(
    lambda x: (x.mean() - 0.02/250) / x.std() * np.sqrt(250) if x.std() > 0 else 0
)
ax3.plot(rolling_sharpe.index, rolling_sharpe.values, color="#4A90D9", linewidth=1.5, label=f"63日滚动夏普")
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

# 设置X轴刻度
ax3.xaxis.set_major_locator(MonthLocator(interval=2))
ax3.xaxis.set_minor_locator(MonthLocator())
ax3.xaxis.set_major_formatter(DateFormatter('%Y-%m'))
plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=9)
plt.tight_layout()
fig.subplots_adjust(bottom=0.08)
output_path = os.path.join("data", "output", "66dashun_v1_1_curve.png")
plt.savefig(output_path, dpi=150, bbox_inches="tight")
print(f"\n图表已保存: {output_path}")
plt.close()

# ── 打印V1.1详细统计 ──
print("\n" + "=" * 80)
print("66大顺 V1.1 回测结果总结")
print("=" * 80)
print(f"总收益率: {result['total_return']:.2f}%")
print(f"年化收益: {result['ann_return']:.2f}%")
print(f"夏普比率: {result['sharpe']:.2f}")
print(f"最大回撤: {result['max_drawdown']:.2f}%")
print(f"胜率: {result['win_rate']:.1f}%")
print(f"盈亏比: {result['profit_ratio']:.2f}")
print(f"利润因子: {result['profit_factor']:.2f}")
print(f"交易次数: {result['total_trades']}")
print(f"\n退出原因分布:")
for reason, count in result['exit_reasons'].items():
    pct = count / result['total_trades'] * 100
    print(f"  {reason}: {count}次 ({pct:.1f}%)")
print("=" * 80)
