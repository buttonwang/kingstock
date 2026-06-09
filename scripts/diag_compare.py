"""诊断脚本：对比plot_66dashun.py和run_ml_backtest.py的信号差异"""
import os, sys
import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

# 加载backtest结果CSV
csv_path = os.path.join("data", "output", "backtest_ml_20230601_20260603.csv")
if os.path.exists(csv_path):
    df_bt = pd.read_csv(csv_path)
    print(f"=== 独立回测CSV ===")
    print(f"信号总数: {len(df_bt)}")
    print(f"唯一日期数: {df_bt['date'].nunique()}")
    print(f"唯一股票数: {df_bt['code'].nunique()}")
    print(f"列: {list(df_bt.columns)}")
    
    # 信号质量
    if 'total_score' in df_bt.columns:
        print(f"total_score 分布: mean={df_bt['total_score'].mean():.1f}, min={df_bt['total_score'].min()}, max={df_bt['total_score'].max()}")
    if 'score_ml' in df_bt.columns:
        print(f"score_ml 分布: mean={df_bt['score_ml'].mean():.1f}, min={df_bt['score_ml'].min():.1f}, max={df_bt['score_ml'].max():.1f}")
        print(f"score_ml==0: {(df_bt['score_ml']==0).sum()} ({(df_bt['score_ml']==0).mean()*100:.1f}%)")
    if 'market_state' in df_bt.columns:
        print(f"市场状态分布: {df_bt['market_state'].value_counts().to_dict()}")
    
    # 每日信号数
    daily_counts = df_bt.groupby('date').size()
    print(f"每日信号数: mean={daily_counts.mean():.1f}, max={daily_counts.max()}, min={daily_counts.min()}")
    print(f"信号数>5的天数: {(daily_counts > 5).sum()}")
    
    # 用backtest的df跑simulate_pure_portfolio，看收益率
    from src.data_fetcher import DataFetcher
    from src.portfolio_manager import simulate_pure_portfolio
    
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
    fetcher.close()
    
    # 交易日：必须从板块日线获取完整的726天，不能仅用有信号的天
    fetcher2 = DataFetcher()
    sec_df = fetcher2._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
    sector_daily = {}
    for _, row in sec_df.iterrows():
        df = fetcher2._sql_to_df(
            "SELECT date FROM sector_daily WHERE sector_name=? AND sector_type=? "
            "AND date>=? AND date<=? ORDER BY date",
            params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
        )
        if not df.empty:
            sector_daily[row["sector_name"]] = df
    fetcher2.close()
    all_dates_set = set()
    for df in sector_daily.values():
        all_dates_set.update(df["date"].tolist())
    start_date_ts, end_date_ts = pd.Timestamp(start_date), pd.Timestamp(end_date)
    trading_dates = sorted(d for d in all_dates_set if start_date_ts <= pd.Timestamp(d) <= end_date_ts)
    print(f"\nstock_daily: {len(stock_daily)} stocks")
    print(f"trading_dates: {len(trading_dates)} days (完整交易日)")
    print(f"信号覆盖日期: {df_bt['date'].nunique()} / {len(trading_dates)}")
    
    # 增强版回测
    print("\n=== 用backtest CSV信号跑增强版组合模拟 ===")
    enhanced_result = simulate_pure_portfolio(
        df_bt, stock_daily, trading_dates=trading_dates,
        dynamic_hold=True, market_state="strong",
        use_price_stop=True, use_partial_take_profit=True,
        use_trailing_stop=True,
    )
    print(f"收益率: {enhanced_result['total_return']:.2f}%")
    print(f"夏普: {enhanced_result['sharpe']:.2f}")
    print(f"最大回撤: {enhanced_result['max_drawdown']:.2f}%")
    print(f"交易次数: {enhanced_result['total_trades']}")
    print(f"胜率: {enhanced_result['win_rate']:.1f}%")
    
    # 对比: 仅用plot脚本的关键列跑
    plot_cols = ["date", "code", "name", "score_ml", "total_score", "max_score", "market_state"]
    available_cols = [c for c in plot_cols if c in df_bt.columns]
    df_slim = df_bt[available_cols].copy()
    print(f"\n=== 用精简列({available_cols})跑组合模拟 ===")
    slim_result = simulate_pure_portfolio(
        df_slim, stock_daily, trading_dates=trading_dates,
        dynamic_hold=True, market_state="strong",
        use_price_stop=True, use_partial_take_profit=True,
        use_trailing_stop=True,
    )
    print(f"收益率: {slim_result['total_return']:.2f}%")
    print(f"交易次数: {slim_result['total_trades']}")
    
else:
    print(f"CSV不存在: {csv_path}")
    print("请先运行 run_ml_backtest.py")
