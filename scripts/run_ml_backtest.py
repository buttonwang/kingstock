"""V1.0 纯信号策略回测：直接从SQLite缓存执行回测（跳过BacktestEngine的增量拉取）

用法:
    python scripts/run_ml_backtest.py [--quick]

核心策略: 纯信号动态持有期模式，按ML评分分档(7/5/3天)，到期自动平仓。
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
from src.indicators.macd import calculate_macd
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.scoring import compute_total_score
from src.ml import ml_scorer
from src.portfolio_manager import simulate_portfolio, simulate_pure_portfolio
from src.utils import setup_logging
from src.market_state import get_market_state, is_tradeable, get_score_threshold, get_ml_min_threshold
from config.settings import (
    RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT, ML_SCORE_MIN_THRESHOLD, OUTPUT_PATH,
    SCORE_THRESHOLD_STRONG, SCORE_THRESHOLD_CHOPPY, MAX_DAILY_OUTPUT,
    PURE_STOP_LOSS_PCT, TAKE_PROFIT_TRIGGER, TAKE_PROFIT_SELL_RATIO,
    ENHANCED_RULES_MIN, WEAK_MARKET_MAX_SIGNALS,
)

logger = setup_logging("run_ml_backtest")
FORWARD_WINDOWS = [2, 10, 30, 60]


def check_weekly_trend(df: pd.DataFrame) -> bool:
    """周线MACD多头确认

    用日线数据聚合出周线，判断MACD是否为多头状态(金叉区域)。
    数据不足12周时放行。
    """
    from src.indicators.macd import calculate_macd

    if df is None or len(df) < 60:
        return True

    df_copy = df.copy()
    df_copy["date"] = pd.to_datetime(df_copy["date"])
    weekly = df_copy.resample("W", on="date").agg({
        "close": "last", "high": "max", "low": "min",
        "open": "first", "volume": "sum",
    }).dropna()

    if len(weekly) < 12:
        return True  # 数据不足，放行

    try:
        macd_w = calculate_macd(weekly)
        if macd_w is not None and len(macd_w) > 0:
            last = macd_w.iloc[-1]
            dif = last.get("dif", 0)
            dea = last.get("dea", 0)
            if pd.notna(dif) and pd.notna(dea):
                return dif > dea  # 周线MACD金叉状态
    except Exception:
        pass

    return True


def run_ml_backtest(quick: bool = False):
    """从SQLite缓存直接运行ML增强回测"""
    fetcher = DataFetcher()

    start_date, end_date = "20230601", "20260603"
    lookback = pd.Timestamp(start_date) - pd.Timedelta(days=250)
    lookfwd = pd.Timestamp(end_date) + pd.Timedelta(days=120)
    fmt_start, fmt_end = lookback.strftime("%Y-%m-%d"), lookfwd.strftime("%Y-%m-%d")

    logger.info("加载缓存数据 %s ~ %s ...", start_date, end_date)

    # 读取板块日线
    sector_df = fetcher._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
    sector_daily = {}
    for _, row in sector_df.iterrows():
        df = fetcher._sql_to_df(
            "SELECT date, close, change_pct FROM sector_daily WHERE sector_name=? AND sector_type=? "
            "AND date>=? AND date<=? ORDER BY date",
            params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
        )
        if not df.empty:
            sector_daily[row["sector_name"]] = df

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

    # ML模型检查
    ml_avail = ml_scorer.is_available()
    logger.info("ML评分: %s", "启用" if ml_avail else "未启用(将跳过)")

    # 主循环
    results = []
    total_dates = len(trading_dates)
    
    # ML评分分布统计
    ml_score_all = []
    ml_score_after_filters = []

    for di, date_str in enumerate(trading_dates):
        if di % max(1, total_dates // 20) == 0:
            logger.info("回测 %d/%d (%.0f%%)", di, total_dates, 100*di/total_dates)

        fmt_date = pd.Timestamp(date_str)

        # 切片板块日线+计算RPS
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

        # 候选池
        code_to_sector = {}
        for name in top_sectors:
            codes = sector_constituents.get(name, set())
            for c in codes:
                code_to_sector.setdefault(c, name)

        # 切片股票日线
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

        # 筛选
        macd_codes = filter_by_macd(stock_dict)
        zjtj_codes = filter_by_zjtj(stock_dict)
        kdj_codes = filter_by_kdj(stock_dict)
        core_codes = macd_codes & zjtj_codes

        # 周线趋势确认过滤
        enhanced_codes = set()
        for code in core_codes:
            sub_df = stock_dict.get(code)
            if sub_df is not None and check_weekly_trend(sub_df):
                enhanced_codes.add(code)
        core_codes = enhanced_codes
        if not core_codes:
            continue

        # 基准计算（所有候选股票的前瞻收益，用于报告）
        bench_2d_list, bench_10d_list, bench_30d_list, bench_60d_list = [], [], [], []
        for code, df in stock_daily.items():
            dates = pd.to_datetime(df["date"])
            idx = None
            for i, d in enumerate(dates):
                if d >= fmt_date:
                    idx = i
                    break
            if idx is None:
                continue
            closes = df["close"].values
            bc = closes[idx]
            if bc == 0:
                continue
            for n, lst in [(2, bench_2d_list), (10, bench_10d_list), (30, bench_30d_list), (60, bench_60d_list)]:
                if idx + n < len(closes):
                    lst.append((closes[idx+n] / bc - 1) * 100)
        bench = {
            "bench_2d": round(np.mean(bench_2d_list), 2) if bench_2d_list else 0,
            "bench_10d": round(np.mean(bench_10d_list), 2) if bench_10d_list else 0,
            "bench_30d": round(np.mean(bench_30d_list), 2) if bench_30d_list else 0,
            "bench_60d": round(np.mean(bench_60d_list), 2) if bench_60d_list else 0,
        }

        # Phase 5: 市场状态判断（基于候选池股票过去10日平均涨幅，无未来偏差）
        past_returns = []
        for code, df in stock_dict.items():
            closes = df["close"].values
            if len(closes) >= 11:
                past_ret = (closes[-1] / closes[-11] - 1) * 100
                past_returns.append(past_ret)
        market_10d_past = np.mean(past_returns) if past_returns else 0
        market_state = get_market_state(market_10d_past)

        # Phase 5: 弱势市场处理（不空仓，降低仓位和收紧门槛）
        if not is_tradeable(market_state):
            # 弱势市场：限制每日信号数
            weak_limit = WEAK_MARKET_MAX_SIGNALS
            market_state_str = "weak_reduced"
        else:
            market_state_str = market_state

        # 记录结果
        for code in core_codes:
            df = stock_dict[code]
            kdj_pass = code in kdj_codes
            sector = code_to_sector[code]
            rps_rank = rps_rank_map.get(sector, RPS_TOP_N)

            # 正面收益、评分
            ml_val = None
            if ml_avail:
                try:
                    ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
                except Exception:
                    pass

            # Phase 4: ML评分阈值过滤
            if ml_val is not None:
                ml_score_all.append(ml_val)
            # Phase 5: 根据市场状态动态调整ML阈值
            ml_threshold = get_ml_min_threshold(market_state)
            if ml_val is not None and ml_val < ml_threshold:
                continue
            if ml_val is not None:
                ml_score_after_filters.append(ml_val)

            score_info = {"total_score": 0, "score_macd": 0, "score_zjtj": 0,
                          "score_kdj": 0, "score_rps": 0, "score_volume": 0,
                          "score_finance": 0, "score_ml": 0}
            enh_info = {"volume_pass": False, "ma_alignment_pass": False, "price_position_pass": False}
            try:
                dm = calculate_macd(df)
                dk = calculate_kdj(df)
                dz = calculate_zjtj(df)
                scores = compute_total_score(dm, dk, dz, rps_rank, ml_score=ml_val)
                score_info.update(scores)
                enh_info.update(check_all_enhanced_rules(df))

                # Phase 4: 入场增强过滤
                if enh_info["rules_passed"] < ENHANCED_RULES_MIN:
                    continue
            except Exception:
                pass

            # 未来收益（用完整 stock_daily[code] 查找未来价格）
            full_df = stock_daily[code]
            full_dates = pd.to_datetime(full_df["date"])
            match_idx = None
            for i, d in enumerate(full_dates):
                if d == fmt_date:
                    match_idx = i
                    break
            if match_idx is None:
                for i, d in enumerate(full_dates):
                    if d >= fmt_date:
                        match_idx = i
                        break
            returns = {n: None for n in FORWARD_WINDOWS}
            full_closes = full_df["close"].values
            if match_idx is not None and full_closes[match_idx] > 0:
                base = full_closes[match_idx]
                for n in FORWARD_WINDOWS:
                    fwd_idx = match_idx + n
                    if fwd_idx < len(full_closes):
                        fwd_price = full_closes[fwd_idx]
                        if fwd_price > 0:
                            returns[n] = round((fwd_price / base - 1) * 100, 2)

            record = {
                "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
                "source_main": True, "source_manual": True, "source_ebk": True,
                "kdj_pass": kdj_pass,
                **score_info, **enh_info,
                "return_2d": returns[2], "bench_2d": bench["bench_2d"],
                "return_10d": returns[10], "bench_10d": bench["bench_10d"],
                "return_30d": returns[30], "bench_30d": bench["bench_30d"],
                "return_60d": returns[60], "bench_60d": bench["bench_60d"],
                # Phase 5: 记录市场状态
                "market_state": market_state_str,
                "market_10d_past": round(market_10d_past, 2),
            }
            results.append(record)

    # 输出结果
    df_result = pd.DataFrame(results)
    csv_path = os.path.join(OUTPUT_PATH, f"backtest_ml_{start_date}_{end_date}.csv")
    df_result.to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info("回测完成: %d 条信号 -> %s", len(df_result), csv_path)

    # ── 组合模拟（止损模式） ──
    logger.info("运行组合模拟 (资金曲线 + 动态止损止盈)...")
    portfolio_result = simulate_portfolio(df_result, stock_daily, trading_dates=trading_dates)

    # ── 组合模拟（纯信号模式，动态持有期） ──
    logger.info("运行 V1.0 纯信号组合模拟 (动态持有期, 按ML评分分档)...")
    pure_result = simulate_pure_portfolio(df_result, stock_daily, trading_dates=trading_dates, dynamic_hold=True)

    # 增强版（市场状态感知 + 价格止损 + 部分止盈）
    logger.info("运行增强版 (市场感知+价格止损+部分止盈)...")
    enhanced_result = simulate_pure_portfolio(
        df_result, stock_daily, trading_dates=trading_dates,
        dynamic_hold=True, market_state="strong",  # 传默认strong以启用增强功能
        use_price_stop=True, use_partial_take_profit=True,
        use_trailing_stop=True,
    )

    # 打印摘要
    if df_result.empty:
        print("  无信号生成")
        print("=" * 80)
        return df_result

    print("\n" + "=" * 80)
    print("  ML增强回测结果")
    print(f"  回测区间: {start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} ~ {end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}")
    print("=" * 80)

    # ── 信号级统计（传统） ──
    print("\n【信号级统计】")
    for col in ["return_2d", "return_10d", "return_30d", "return_60d"]:
        v = df_result[col].dropna()
        if not v.empty:
            print(f"  {col:<12} 均涨={v.mean():+.2f}%  胜率={(v>0).mean()*100:.1f}% (n={len(v)})")

    # ── 组合级结果（止损模式） ──
    print("\n【组合模拟结果 (仓位管理+动态止损止盈)】")
    pr = portfolio_result
    print(f"  初始资金: 1,000,000")
    print(f"  最终资产: {pr['final_value']:,.2f}")
    print(f"  总收益率: {pr['total_return']:+.2f}%")
    print(f"  年化收益: {pr['ann_return']:+.2f}%")
    print(f"  最大回撤: {pr['max_drawdown']:.2f}%")
    print(f"  夏普比率: {pr['sharpe']:.2f}")
    print(f"  交易次数: {pr['total_trades']}")
    print(f"  胜率: {pr['win_rate']:.1f}%")
    print(f"  盈亏比: {pr['profit_ratio']:.2f}")
    print(f"  Profit Factor: {pr['profit_factor']:.2f}")

    # ── 退出原因分布 ──
    if pr["exit_reasons"]:
        print("\n【退出原因分布】")
        for reason, count in sorted(pr["exit_reasons"].items(), key=lambda x: -x[1]):
            reason_name = {
               "HARD_STOP": "硬止损", "TRAILING_STOP": "跟踪止损",
                "ATR_HARD_STOP": "ATR硬止损", "ATR_TRAILING_STOP": "ATR跟踪止损",
                "TIME_STOP": "时间止损", "SCORE_STOP": "ML评分失效",
                "NO_DATA": "数据不足", "FORCED_CLOSE": "期末强制平仓",
                "PRICE_STOP": "价格止损", "PARTIAL_TP": "部分止盈",
                "TRAILING_STOP": "移动止盈",
            }.get(reason, reason)
            print(f"  {reason_name}: {count}次")

    # ── 纯信号模式结果 ──
    print("\n【V1.0 纯信号组合模拟 (动态持有期, 按ML评分分档)】")
    pu = pure_result
    print(f"  初始资金: 1,000,000")
    print(f"  最终资产: {pu['final_value']:,.2f}")
    print(f"  总收益率: {pu['total_return']:+.2f}%")
    print(f"  年化收益: {pu['ann_return']:+.2f}%")
    print(f"  最大回撤: {pu['max_drawdown']:.2f}%")
    print(f"  夏普比率: {pu['sharpe']:.2f}")
    print(f"  交易次数: {pu['total_trades']}")
    print(f"  胜率: {pu['win_rate']:.1f}%")
    print(f"  盈亏比: {pu['profit_ratio']:.2f}")
    print(f"  Profit Factor: {pu['profit_factor']:.2f}")

    # 增强版结果
    en = enhanced_result
    print("\n【增强版 (市场感知+价格止损+部分止盈)】")
    print(f"  初始资金: 1,000,000")
    print(f"  最终资产: {en['final_value']:,.2f}")
    print(f"  总收益率: {en['total_return']:+.2f}%")
    print(f"  年化收益: {en['ann_return']:+.2f}%")
    print(f"  最大回撤: {en['max_drawdown']:.2f}%")
    print(f"  夏普比率: {en['sharpe']:.2f}")
    print(f"  交易次数: {en['total_trades']}")
    print(f"  胜率: {en['win_rate']:.1f}%")
    print(f"  盈亏比: {en['profit_ratio']:.2f}")
    print(f"  Profit Factor: {en['profit_factor']:.2f}")

    # ── 三模式对比 ──
    print("\n" + "=" * 80)
    print("  【核心对比：止损模式 vs V1.0纯信号 vs 增强版】")
    print("=" * 80)
    print(f"  {'指标':<16} {'止损模式':>16} {'V1.0纯信号':>16} {'增强版':>16}")
    print(f"  {'─'*14:<16} {'─'*14:>16} {'─'*14:>16} {'─'*14:>16}")
    print(f"  {'总收益率':<16} {pr['total_return']:>+16.2f}% {pu['total_return']:>+16.2f}% {en['total_return']:>+16.2f}%")
    print(f"  {'年化收益':<16} {pr['ann_return']:>+16.2f}% {pu['ann_return']:>+16.2f}% {en['ann_return']:>+16.2f}%")
    print(f"  {'最大回撤':<16} {pr['max_drawdown']:>16.2f}% {pu['max_drawdown']:>16.2f}% {en['max_drawdown']:>16.2f}%")
    print(f"  {'夏普比率':<16} {pr['sharpe']:>16.2f} {pu['sharpe']:>16.2f} {en['sharpe']:>16.2f}")
    print(f"  {'胜率':<16} {pr['win_rate']:>16.1f}% {pu['win_rate']:>16.1f}% {en['win_rate']:>16.1f}%")
    print(f"  {'交易次数':<16} {pr['total_trades']:>16} {pu['total_trades']:>16} {en['total_trades']:>16}")

    # 分析ML评分的区分度
    if ml_score_all:
        scores = np.array(ml_score_all)
        logger.info("总ML评分分布: 总数=%d, min=%.1f, max=%.1f, mean=%.1f, median=%.1f",
                     len(scores), scores.min(), scores.max(), scores.mean(), np.median(scores))
        for bs in range(0, 16, 3):
            be = min(bs + 2, 15)
            cnt = np.sum((scores >= bs) & (scores <= be))
            if cnt > 0:
                logger.info("  ML评分 [%d-%d]: %d (%.1f%%)", bs, be, cnt, cnt/len(scores)*100)
    if ml_score_after_filters:
        af = np.array(ml_score_after_filters)
        logger.info("过滤后ML评分: 总数=%d, min=%.1f, max=%.1f", len(af), af.min(), af.max())

    if "score_ml" in df_result.columns and df_result["score_ml"].nunique() > 1:
        print("\n【ML评分分段分析】")
        for bucket, label in [(0, "0分"), (1, "1-5分"), (6, "6-10分"), (11, "11-15分")]:
            if bucket == 0:
                sub = df_result[df_result["score_ml"] == 0]
            else:
                next_buckets = [b for b in [0, 1, 6, 11] if b > bucket]
                next_min = min(next_buckets) if next_buckets else 16
                sub = df_result[(df_result["score_ml"] >= bucket) & (df_result["score_ml"] < next_min)]
            if not sub.empty:
                r = sub["return_2d"].dropna()
                print(f"  ML评分{label}: n={len(sub)}, 2日均涨={r.mean():+.2f}%, 胜率={(r>0).mean()*100:.1f}%")

    # 市场状态分布统计
    if "market_state" in df_result.columns:
        print("\n【市场状态分布统计】")
        for state in ["strong", "choppy"]:  # weak已被过滤
            sub = df_result[df_result["market_state"] == state]
            if not sub.empty:
                r = sub["return_2d"].dropna()
                print(f"  市场{state}: {len(sub)}条信号, 2日均涨={r.mean():+.2f}%, 胜率={(r>0).mean()*100:.1f}%")

    # 退出原因分布（增强版）
    if en["exit_reasons"]:
        print("\n【增强版退出原因分布】")
        for reason, count in sorted(en["exit_reasons"].items(), key=lambda x: -x[1]):
            reason_name = {
                "PURE_HOLD": "正常持有到期", "PRICE_STOP": "价格止损",
                "PARTIAL_TP": "部分止盈", "TRAILING_STOP": "移动止盈",
                "NO_DATA": "数据不足",
                "FORCED_CLOSE": "期末强制平仓",
            }.get(reason, reason)
            print(f"  {reason_name}: {count}次")

    return df_result


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    run_ml_backtest(quick=quick)
