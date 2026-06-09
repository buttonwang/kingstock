"""分析66大顺回测中收益最高的前20只个股"""
import os, sys
import numpy as np
import pandas as pd

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

start_date, end_date = "20230601", "20260603"

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

# 交易日列表
all_dates = set()
for df in sector_daily.values():
    all_dates.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates
                       if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))

# ── 信号生成 ──
results = []
from src.scoring import compute_total_score
from src.indicators.macd import calculate_macd
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.ml import ml_scorer

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

for di, date_str in enumerate(trading_dates):
    if di % max(1, total_dates // 20) == 0:
        print(f"信号生成 {di}/{total_dates} ({100*di//total_dates}%)")
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
            "score_ml": scores.get("score_ml", 0),
            "total_score": scores.get("total_score", 0),
            "max_score": scores.get("max_score", 100),
            "market_state": market_state_str,
        })

df_result = pd.DataFrame(results)
print(f"\n信号总数: {len(df_result)}")

# ── 组合模拟 ──
result = simulate_pure_portfolio(
    df_result,
    stock_daily, trading_dates=trading_dates,
    dynamic_hold=True,
)

# ── 分析每只股票的交易记录 ──
closed_trades = result.get("closed_trades", [])
if not closed_trades:
    print("没有交易记录")
    sys.exit(1)

trades_df = pd.DataFrame(closed_trades)

# 添加股票名称和板块信息
trades_df["name"] = trades_df["code"].map(stock_name_map)
trades_df["sector"] = trades_df["code"].map(
    {code: sector for sector, codes in sector_constituents.items() for code in codes}
)

# 计算每笔交易的收益率
trades_df["return_pct"] = ((trades_df["exit_price"] / trades_df["entry_price"] - 1) * 100).round(2)

# 按收益率排序，取前20
top20 = trades_df.nlargest(20, "return_pct")

print("\n" + "="*100)
print("66大顺 V1.0 回测收益最高的前20只个股")
print("="*100)
print(f"{'排名':<5} {'代码':<10} {'名称':<15} {'板块':<20} {'收益率':>10} {'买入日期':<12} {'卖出日期':<12} {'持有天数':>8} {'ML评分':>8}")
print("-"*100)

for i, (_, trade) in enumerate(top20.iterrows(), 1):
    code = trade["code"]
    name = trade.get("name", "未知")
    sector = trade.get("sector", "未知")
    ret = trade["return_pct"]
    entry_date = trade["entry_date"][:10] if len(str(trade["entry_date"])) > 10 else trade["entry_date"]
    exit_date = trade["exit_date"][:10] if len(str(trade["exit_date"])) > 10 else trade["exit_date"]
    held_days = trade.get("held_days", 0)
    ml_score = trade.get("entry_score_ml", 0)
    
    print(f"{i:<5} {code:<10} {name:<15} {sector:<20} {ret:>9.2f}% {entry_date:<12} {exit_date:<12} {held_days:>8} {ml_score:>8.0f}")

print("="*100)

# 保存为CSV
output_path = "data/output/top20_stocks.csv"
top20.to_csv(output_path, index=False, encoding="utf-8-sig")
print(f"\n详细数据已保存: {output_path}")

# 统计分析
print("\n前20只个股统计:")
print(f"  平均收益率: {top20['return_pct'].mean():.2f}%")
print(f"  最高收益率: {top20['return_pct'].max():.2f}%")
print(f"  最低收益率: {top20['return_pct'].min():.2f}%")
print(f"  平均持有天数: {top20['held_days'].mean():.1f}天")
print(f"  平均ML评分: {top20['entry_score_ml'].mean():.1f}")

# 板块分布
print("\n板块分布:")
sector_counts = top20["sector"].value_counts()
for sector, count in sector_counts.items():
    print(f"  {sector}: {count}只")
