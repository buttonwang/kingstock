"""66大顺对比回测：三种买入策略对比

所有策略均使用66大顺 V1.0组合模拟框架（纯信号动态持有期）。
S1: RPS 5日最强板块 + ZJTJ策略
S2: RPS 5日最强板块 + KDJ + 均线多头排列
S3: MACD∩ZJTJ核心信号（无ML评分过滤）

用法:
    python scripts/run_66dashun_comparison.py [--quick]
"""
import os
import sys
import warnings
from collections import defaultdict

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.data_fetcher import DataFetcher
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.indicators.enhanced_rules import check_ma_alignment, check_all_enhanced_rules
from src.portfolio_manager import simulate_pure_portfolio
from src.utils import setup_logging
from config.settings import (
    RPS_TOP_N_STRICT, OUTPUT_PATH,
    ENHANCED_RULES_MIN,
)

logger = setup_logging("run_66dashun_comparison")
FORWARD_WINDOWS = [2, 10, 30, 60]


def check_weekly_trend(df):
    """周线MACD多头确认（同 run_ml_backtest.py）"""
    if df is None or len(df) < 60:
        return True
    from src.indicators.macd import calculate_macd
    df_w = df.copy()
    df_w["date"] = pd.to_datetime(df_w["date"])
    df_w = df_w.set_index("date").resample("W-FRI").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum",
    }).dropna()
    if len(df_w) < 12:
        return True
    dm = calculate_macd(df_w)
    if dm is None or "macd" not in dm.columns or "dif" not in dm.columns or "dea" not in dm.columns:
        return True
    macd_val = dm["macd"].values[-1]
    dif_val = dm["dif"].values[-1]
    dea_val = dm["dea"].values[-1]
    return macd_val > 0 and dif_val > dea_val


def run_66dashun_comparison(quick=False):
    start_date = "20230601"
    end_date = "20260603"

    # ── 加载缓存数据 ──
    fetcher = DataFetcher()
    logger.info("加载缓存数据 %s ~ %s ...", start_date, end_date)
    fmt_start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    fmt_end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    # 读取板块日线
    sec_df = fetcher._sql_to_df("SELECT date, sector_name, close FROM sector_daily WHERE date>=? AND date<=? ORDER BY date",
                                 params=(fmt_start, fmt_end))
    sector_daily = {}
    for _, row in sec_df.iterrows():
        sector_daily.setdefault(row["sector_name"], []).append({
            "date": row["date"], "close": row["close"],
        })
    sector_daily = {k: pd.DataFrame(v) for k, v in sector_daily.items()}

    # 读取板块成分股
    cons_df = fetcher._sql_to_df("SELECT sector_name, code, name FROM sector_constituents")
    sector_constituents, stock_name_map = {}, {}
    for _, row in cons_df.iterrows():
        n = row["sector_name"]
        sector_constituents.setdefault(n, set()).add(row["code"])
        stock_name_map[row["code"]] = row["name"]

    # 读取股票日线
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
    logger.info("缓存: %d板块, %d股票(>=60天)", len(sector_daily), len(stock_daily))

    # 交易日列表
    all_dates = set()
    for df in sector_daily.values():
        all_dates.update(df["date"].tolist())
    trading_dates = sorted(d for d in all_dates
                           if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))
    logger.info("交易日: %d (%s ~ %s)", len(trading_dates), trading_dates[0], trading_dates[-1])

    if quick and len(trading_dates) > 30:
        trading_dates = trading_dates[-len(trading_dates)//5:]
        logger.info("快速模式: %d交易日", len(trading_dates))

    # ── 三组信号收集器 ──
    signals_1 = []  # RPS 5日 + ZJTJ
    signals_2 = []  # RPS 5日 + KDJ + 均线多头
    signals_3 = []  # MACD∩ZJTJ核心信号

    total_dates = len(trading_dates)

    for di, date_str in enumerate(trading_dates):
        if di % max(1, total_dates // 20) == 0:
            logger.info("回测 %d/%d (%.0f%%)", di, total_dates, 100*di/total_dates)

        fmt_date = pd.Timestamp(date_str)

        # ── 切片板块日线 ──
        sdata_20 = {}  # 20日RPS用
        sdata_5 = {}   # 5日RPS用
        for name, df in sector_daily.items():
            sub = df[pd.to_datetime(df["date"]) <= fmt_date]
            if len(sub) >= 20:
                sdata_20[name] = sub
            if len(sub) >= 5:
                sdata_5[name] = sub

        if not sdata_5:
            continue

        # ── S1/S2: RPS 5日最强板块 ──
        try:
            rps_5d = calculate_sector_rps(sdata_5, period=5)
            top_sectors_5d = get_top_sectors(rps_5d, top_n=RPS_TOP_N_STRICT)
        except Exception:
            top_sectors_5d = []
        if not top_sectors_5d:
            continue

        # ── S3: RPS 20日最强板块（沿用现有逻辑） ──
        try:
            rps_20d = calculate_sector_rps(sdata_20, period=20)
            top_sectors_20d = get_top_sectors(rps_20d, top_n=RPS_TOP_N_STRICT)
        except Exception:
            top_sectors_20d = []

        # ── S1/S2 候选池（5日RPS板块） ──
        code_to_sector_5 = {}
        for name in top_sectors_5d:
            for c in sector_constituents.get(name, set()):
                code_to_sector_5.setdefault(c, name)
        if not code_to_sector_5:
            continue

        # ── S3 候选池（20日RPS板块） ──
        code_to_sector_20 = {}
        for name in top_sectors_20d:
            for c in sector_constituents.get(name, set()):
                code_to_sector_20.setdefault(c, name)

        # 切片股票日线
        stock_dict_5 = {}
        for code in code_to_sector_5:
            df = stock_daily.get(code)
            if df is None:
                continue
            sub = df[pd.to_datetime(df["date"]) <= fmt_date].copy()
            if len(sub) >= 60:
                stock_dict_5[code] = sub

        stock_dict_20 = {}
        all_s3_codes = code_to_sector_20.keys() & stock_daily.keys()
        for code in all_s3_codes:
            df = stock_daily.get(code)
            if df is None:
                continue
            sub = df[pd.to_datetime(df["date"]) <= fmt_date].copy()
            if len(sub) >= 60:
                stock_dict_20[code] = sub

        if not stock_dict_5:
            continue

        # ── 执行筛选 ──
        macd_codes = filter_by_macd(stock_dict_20) if stock_dict_20 else set()
        zjtj_codes_s5 = filter_by_zjtj(stock_dict_5)
        zjtj_codes_s20 = filter_by_zjtj(stock_dict_20) if stock_dict_20 else set()
        kdj_codes_s5 = filter_by_kdj(stock_dict_5)

        # ── S1: RPS 5日 + ZJTJ ──
        for code in (stock_dict_5.keys() & zjtj_codes_s5):
            if code not in stock_dict_5:
                continue
            sub_df = stock_dict_5[code]
            if check_weekly_trend(sub_df):
                signals_1.append({
                    "date": date_str, "code": code,
                    "name": stock_name_map.get(code, ""),
                    "score_ml": 0,
                })

        # ── S2: RPS 5日 + KDJ + 均线多头排列 ──
        for code in (stock_dict_5.keys() & kdj_codes_s5):
            if code not in stock_dict_5:
                continue
            sub_df = stock_dict_5[code]
            if check_ma_alignment(sub_df) and check_weekly_trend(sub_df):
                signals_2.append({
                    "date": date_str, "code": code,
                    "name": stock_name_map.get(code, ""),
                    "score_ml": 0,
                })

        # ── S3: MACD∩ZJTJ核心信号（无ML过滤） ──
        core_codes = macd_codes & zjtj_codes_s20
        for code in core_codes:
            if code not in stock_dict_20:
                continue
            sub_df = stock_dict_20[code]
            if check_weekly_trend(sub_df):
                signals_3.append({
                    "date": date_str, "code": code,
                    "name": stock_name_map.get(code, ""),
                    "score_ml": 0,
                })

    # ── 构建三组信号DataFrame ──
    df_1 = pd.DataFrame(signals_1) if signals_1 else pd.DataFrame()
    df_2 = pd.DataFrame(signals_2) if signals_2 else pd.DataFrame()
    df_3 = pd.DataFrame(signals_3) if signals_3 else pd.DataFrame()

    logger.info("信号数: S1(RPS5+ZJTJ)=%d, S2(RPS5+KDJ+MA)=%d, S3(MACD∩ZJTJ)=%d",
                len(df_1), len(df_2), len(df_3))

    # ── 组合模拟（66大顺 V1.0：纯信号动态持有期） ──
    logger.info("运行S1组合模拟 (RPS5+ZJTJ)...")
    result_1 = simulate_pure_portfolio(
        df_1[["date", "code", "name", "score_ml"]],
        stock_daily, trading_dates=trading_dates,
        dynamic_hold=True,
    ) if not df_1.empty else {"total_trades": 0, "final_value": 1_000_000, "total_return": 0}

    logger.info("运行S2组合模拟 (RPS5+KDJ+均线多头)...")
    result_2 = simulate_pure_portfolio(
        df_2[["date", "code", "name", "score_ml"]],
        stock_daily, trading_dates=trading_dates,
        dynamic_hold=True,
    ) if not df_2.empty else {"total_trades": 0, "final_value": 1_000_000, "total_return": 0}

    logger.info("运行S3组合模拟 (MACD∩ZJTJ核心)...")
    result_3 = simulate_pure_portfolio(
        df_3[["date", "code", "name", "score_ml"]],
        stock_daily, trading_dates=trading_dates,
        dynamic_hold=True,
    ) if not df_3.empty else {"total_trades": 0, "final_value": 1_000_000, "total_return": 0}

    # ── 打印结果 ──
    print("\n" + "=" * 100)
    print("  「66大顺」三种买入策略对比回测结果")
    print(f"  回测区间: {start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} ~ "
          f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}")
    print("=" * 100)
    print(f"  {'指标':<18} {'S1·RPS5+ZJTJ':>18} {'S2·RPS5+KDJ+MA':>18} {'S3·MACD∩ZJTJ':>18}")
    print(f"  {'─'*16:<18} {'─'*16:>18} {'─'*16:>18} {'─'*16:>18}")
    print(f"  {'信号总数':<18} {len(df_1):>18} {len(df_2):>18} {len(df_3):>18}")
    print(f"  {'总收益率%':<18} {result_1.get('total_return',0):>+18.2f} {result_2.get('total_return',0):>+18.2f} {result_3.get('total_return',0):>+18.2f}")
    print(f"  {'年化收益%':<18} {result_1.get('ann_return',0):>+18.2f} {result_2.get('ann_return',0):>+18.2f} {result_3.get('ann_return',0):>+18.2f}")
    print(f"  {'最大回撤%':<18} {result_1.get('max_drawdown',0):>18.2f} {result_2.get('max_drawdown',0):>18.2f} {result_3.get('max_drawdown',0):>18.2f}")
    print(f"  {'夏普比率':<18} {result_1.get('sharpe',0):>18.2f} {result_2.get('sharpe',0):>18.2f} {result_3.get('sharpe',0):>18.2f}")
    print(f"  {'胜率%':<18} {result_1.get('win_rate',0):>18.1f} {result_2.get('win_rate',0):>18.1f} {result_3.get('win_rate',0):>18.1f}")
    print(f"  {'交易次数':<18} {result_1.get('total_trades',0):>18} {result_2.get('total_trades',0):>18} {result_3.get('total_trades',0):>18}")
    print(f"  {'盈亏比':<18} {result_1.get('profit_ratio',0):>18.2f} {result_2.get('profit_ratio',0):>18.2f} {result_3.get('profit_ratio',0):>18.2f}")
    print(f"  {'Profit Factor':<18} {result_1.get('profit_factor',0):>18.2f} {result_2.get('profit_factor',0):>18.2f} {result_3.get('profit_factor',0):>18.2f}")
    print("=" * 100)

    # ── 策略说明 ──
    print("\n【策略说明】")
    print("  S1·RPS5+ZJTJ:    RPS 5日最强板块 → ZJTJ控盘信号 → 66大顺动态持有期")
    print("  S2·RPS5+KDJ+MA:  RPS 5日最强板块 → KDJ买入信号 → 均线多头排列 → 66大顺动态持有期")
    print("  S3·MACD∩ZJTJ:    RPS 20日最强板块 → MACD∩ZJTJ核心信号 → 66大顺动态持有期")
    print("  所有策略共用66大顺 V1.0组合模拟框架（按ML评分动态持有，到期平仓，无止损止盈）")
    print(f"\n  参考: 66大顺基准 (ML评分+动态持有) = 夏普1.53, 收益+46.90%, 信号551")

    # ── 保存结果 ──
    results_path = os.path.join(OUTPUT_PATH, "66dashun_comparison_result.txt")
    with open(results_path, "w", encoding="utf-8") as f:
        f.write("「66大顺」三种买入策略对比回测结果\n")
        f.write(f"回测区间: {start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} ~ {end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}\n\n")
        f.write(f"S1(RPS5+ZJTJ): 信号={len(df_1)}, 收益={result_1.get('total_return',0):+.2f}%, 夏普={result_1.get('sharpe',0):.2f}, 回撤={result_1.get('max_drawdown',0):.2f}%, 胜率={result_1.get('win_rate',0):.1f}%, 交易={result_1.get('total_trades',0)}\n")
        f.write(f"S2(RPS5+KDJ+MA): 信号={len(df_2)}, 收益={result_2.get('total_return',0):+.2f}%, 夏普={result_2.get('sharpe',0):.2f}, 回撤={result_2.get('max_drawdown',0):.2f}%, 胜率={result_2.get('win_rate',0):.1f}%, 交易={result_2.get('total_trades',0)}\n")
        f.write(f"S3(MACD∩ZJTJ): 信号={len(df_3)}, 收益={result_3.get('total_return',0):+.2f}%, 夏普={result_3.get('sharpe',0):.2f}, 回撤={result_3.get('max_drawdown',0):.2f}%, 胜率={result_3.get('win_rate',0):.1f}%, 交易={result_3.get('total_trades',0)}\n")
    logger.info("结果已保存: %s", results_path)

    return df_1, df_2, df_3, result_1, result_2, result_3


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    run_66dashun_comparison(quick=quick)
