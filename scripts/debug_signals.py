"""快速调试：为什么CSV信号+完整交易日=0交易"""
import os, sys, pandas as pd, numpy as np
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.portfolio_manager import simulate_pure_portfolio, _build_signal_lookup

# 加载信号
df_bt = pd.read_csv("data/output/backtest_ml_20230601_20260603.csv")
df_bt["date"] = pd.to_datetime(df_bt["date"]).dt.strftime("%Y-%m-%d")
df_bt["code"] = df_bt["code"].astype(str).str.zfill(6)  # CSV读取后code可能变成int
print(f"信号数: {len(df_bt)}, 日期范围: {df_bt['date'].min()} ~ {df_bt['date'].max()}")

# 加载stock_daily
fetcher = DataFetcher()
start_date, end_date = "20230601", "20260603"
lookback = pd.Timestamp(start_date) - pd.Timedelta(days=250)
lookfwd = pd.Timestamp(end_date) + pd.Timedelta(days=120)
fmt_start = lookback.strftime("%Y-%m-%d")
fmt_end = lookfwd.strftime("%Y-%m-%d")

all_codes = set()
cons_df = fetcher._sql_to_df("SELECT sector_name, code, name FROM sector_constituents")
for _, row in cons_df.iterrows():
    all_codes.add(row["code"])

stock_daily = {}
for code in all_codes:
    df = fetcher._sql_to_df(
        "SELECT date, open, high, low, close, volume, turnover_rate FROM stock_daily "
        "WHERE code=? AND date>=? AND date<=? ORDER BY date",
        params=(code, fmt_start, fmt_end),
    )
    if not df.empty and len(df) >= 60:
        stock_daily[code] = df

# 完整交易日
sec_df = fetcher._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
sector_daily = {}
for _, row in sec_df.iterrows():
    df = fetcher._sql_to_df(
        "SELECT date FROM sector_daily WHERE sector_name=? AND sector_type=? AND date>=? AND date<=? ORDER BY date",
        params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
    )
    if not df.empty:
        sector_daily[row["sector_name"]] = df
fetcher.close()

all_dates_set = set()
for df in sector_daily.values():
    all_dates_set.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates_set
                       if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))
trading_dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in trading_dates]
print(f"stock_daily: {len(stock_daily)}, trading_dates: {len(trading_dates)}")
print(f"trading_dates[0:3]: {trading_dates[:3]}, [-3:]: {trading_dates[-3:]}")

# 检查信号是否在stock_daily中
signal_codes = set(df_bt["code"].unique())
available_codes = set(stock_daily.keys())
matched = signal_codes & available_codes
print(f"\n信号股票: {len(signal_codes)}, stock_daily股票: {len(available_codes)}, 匹配: {len(matched)}")
missing = signal_codes - available_codes
if missing:
    print(f"缺失股票(前5): {list(missing)[:5]}")

# 检查信号lookup
signals = df_bt.to_dict("records")
signal_lookup = _build_signal_lookup(signals)
print(f"\nsignal_lookup天数: {len(signal_lookup)}")
# 检查第一个信号日期是否在trading_dates中
first_signal_date = sorted(signal_lookup.keys())[0]
print(f"第一个信号日期: {first_signal_date}")
print(f"在trading_dates中? {first_signal_date in trading_dates}")

# 检查_get_price_at_date
from src.portfolio_manager import _get_price_at_date
sample_code = list(matched)[0] if matched else None
if sample_code:
    price = _get_price_at_date(stock_daily[sample_code], first_signal_date)
    print(f"\n示例: {sample_code} @ {first_signal_date} = price {price}")
    # 检查stock_daily的date格式
    print(f"stock_daily[{sample_code}].date[0]: {repr(stock_daily[sample_code]['date'].iloc[0])}")

# 手动模拟第一天
print(f"\n=== 手动检查第一个信号日 ===")
for date_str in trading_dates:
    day_signals = signal_lookup.get(date_str, [])
    if day_signals:
        print(f"日期: {date_str}, 信号数: {len(day_signals)}")
        for sig in day_signals[:3]:
            code = sig["code"]
            price = _get_price_at_date(stock_daily.get(code, pd.DataFrame()), date_str) if code in stock_daily else -1
            print(f"  code={code}, total_score={sig.get('total_score')}, score_ml={sig.get('score_ml')}, price={price}")
        break

# 直接调用simulate
print(f"\n=== 运行simulate_pure_portfolio ===")
result = simulate_pure_portfolio(
    df_bt, stock_daily, trading_dates=trading_dates,
    dynamic_hold=True, market_state="strong",
    use_price_stop=True, use_partial_take_profit=True,
    use_trailing_stop=True,
)
print(f"收益率: {result['total_return']:.2f}%")
print(f"交易次数: {result['total_trades']}")
if result['closed_trades']:
    for t in result['closed_trades'][:3]:
        print(f"  交易: {t['code']} {t['entry_date']}->{t['exit_date']} ret={t['return_pct']}% reason={t['exit_reason']}")
