"""三策略对比回测：A=当前策略 vs B=纯ML评分 vs C=ML+动态持有期

用法:
    python scripts/run_comparison_backtest.py [--quick]

直接在同一个交易日循环中生成三组信号，确保公平对比。
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
from src.portfolio_manager import simulate_pure_portfolio
from src.utils import setup_logging
from src.market_state import get_market_state, is_tradeable, get_ml_min_threshold, get_max_daily_signals
from config.settings import (
    RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT, ML_SCORE_MIN_THRESHOLD, OUTPUT_PATH,
    SCORE_THRESHOLD_STRONG, SCORE_THRESHOLD_CHOPPY, MAX_DAILY_OUTPUT,
    ENHANCED_RULES_MIN, WEAK_MARKET_MAX_SIGNALS,
)

logger = setup_logging("run_comparison_backtest")
FORWARD_WINDOWS = [2, 10, 30, 60]
MAX_DAILY_ML = 5  # B/C策略每日最大信号数


def check_weekly_trend(df):
    """周线MACD多头确认（同 run_ml_backtest.py）"""
    if df is None or len(df) < 60:
        return True
    from src.indicators.macd import calculate_macd
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


def run_comparison(quick=False):
    """运行三策略对比回测"""
    fetcher = DataFetcher()

    start_date, end_date = "20230601", "20260603"
    lookback = pd.Timestamp(start_date) - pd.Timedelta(days=250)
    lookfwd = pd.Timestamp(end_date) + pd.Timedelta(days=120)
    fmt_start, fmt_end = lookback.strftime("%Y-%m-%d"), lookfwd.strftime("%Y-%m-%d")

    logger.info("加载缓存数据 %s ~ %s ...", start_date, end_date)

    # ── 读取板块日线 ──
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

    # ── 读取板块成分股 ──
    cons_df = fetcher._sql_to_df("SELECT sector_name, code, name FROM sector_constituents")
    sector_constituents, stock_name_map = {}, {}
    for _, row in cons_df.iterrows():
        n = row["sector_name"]
        sector_constituents.setdefault(n, set()).add(row["code"])
        stock_name_map[row["code"]] = row["name"]

    # ── 读取股票日线 ──
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

    # ── 交易日列表 ──
    all_dates = set()
    for df in sector_daily.values():
        all_dates.update(df["date"].tolist())
    trading_dates = sorted(d for d in all_dates
                           if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))
    logger.info("交易日: %d (%s ~ %s)", len(trading_dates), trading_dates[0], trading_dates[-1])

    if quick and len(trading_dates) > 30:
        trading_dates = trading_dates[-len(trading_dates)//5:]
        logger.info("快速模式: %d交易日", len(trading_dates))

    # ── ML模型检查 ──
    ml_avail = ml_scorer.is_available()
    logger.info("ML评分: %s", "启用" if ml_avail else "未启用")

    # ── 三组信号收集 ──
    signals_a = []  # 当前策略: RPS → MACD∩ZJTJ → ML阈值过滤
    signals_b = []  # 纯ML: RPS → ML评分Top 5
    signals_c = []  # ML+动态持有: RPS → ML评分Top 5 + 动态持有

    total_dates = len(trading_dates)

    for di, date_str in enumerate(trading_dates):
        if di % max(1, total_dates // 20) == 0:
            logger.info("回测 %d/%d (%.0f%%)", di, total_dates, 100*di/total_dates)

        fmt_date = pd.Timestamp(date_str)

        # ── RPS计算 ──
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

        # ── 候选池（RPS TOP板块内所有股票） ──
        code_to_sector = {}
        for name in top_sectors:
            codes = sector_constituents.get(name, set())
            for c in codes:
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

        # ── 技术指标过滤（仅用于策略A） ──
        macd_codes = filter_by_macd(stock_dict)
        zjtj_codes = filter_by_zjtj(stock_dict)
        kdj_codes = filter_by_kdj(stock_dict)
        core_codes = macd_codes & zjtj_codes

        # 周线趋势确认
        enhanced_codes = set()
        for code in core_codes:
            sub_df = stock_dict.get(code)
            if sub_df is not None and check_weekly_trend(sub_df):
                enhanced_codes.add(code)
        core_codes = enhanced_codes

        # ── 市场状态 ──
        past_returns = []
        for code, df in stock_dict.items():
            closes = df["close"].values
            if len(closes) >= 11:
                past_ret = (closes[-1] / closes[-11] - 1) * 100
                past_returns.append(past_ret)
        market_10d_past = np.mean(past_returns) if past_returns else 0
        market_state = get_market_state(market_10d_past)
        if not is_tradeable(market_state):
            market_state_str = "weak_reduced"
        else:
            market_state_str = market_state

        # ── 对候选池内所有股票计算ML评分（供B/C使用） ──
        code_ml_scores = {}       # {code: ml_score}
        code_info = {}            # {code: {sector, rps_rank, kdj_pass, dm, dk, dz, ...}}
        for code, df in stock_dict.items():
            sector = code_to_sector[code]
            rps_rank = rps_rank_map.get(sector, RPS_TOP_N)

            kdj_pass = code in kdj_codes
            ml_val = None
            if ml_avail:
                try:
                    ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
                except Exception:
                    pass

            code_ml_scores[code] = ml_val
            code_info[code] = {
                "sector": sector,
                "rps_rank": rps_rank,
                "kdj_pass": kdj_pass,
            }

            # ── 策略A也需要这些信息，预计算 ──
            if code in core_codes:
                try:
                    dm = calculate_macd(df)
                    dk = calculate_kdj(df)
                    dz = calculate_zjtj(df)
                    code_info[code]["dm"] = dm
                    code_info[code]["dk"] = dk
                    code_info[code]["dz"] = dz
                except Exception:
                    pass

        # ═══ 策略A: 当前流程 ═══
        if core_codes:
            # 策略A信号计算（同现有逻辑）
            a_candidates = []
            for code in core_codes:
                info = code_info[code]
                ml_val = code_ml_scores.get(code)

                # ML阈值过滤（按市场状态）
                ml_threshold = get_ml_min_threshold(market_state)
                if ml_val is not None and ml_val < ml_threshold:
                    continue

                sector = info["sector"]
                rps_rank = info["rps_rank"]
                dm = info.get("dm")
                dk = info.get("dk")
                dz = info.get("dz")
                if dm is None or dk is None or dz is None:
                    continue

                scores = compute_total_score(dm, dk, dz, rps_rank, ml_score=ml_val)
                enh_info = check_all_enhanced_rules(df)
                if enh_info["rules_passed"] < ENHANCED_RULES_MIN:
                    continue

                a_candidates.append({
                    "code": code, "name": stock_name_map.get(code, ""),
                    "ml_score": ml_val,
                    "total_score": scores["total_score"],
                    "kdj_pass": info["kdj_pass"],
                })

            # Phase 5: 按市场状态动态确定每日信号上限
            if not is_tradeable(market_state):
                daily_limit = WEAK_MARKET_MAX_SIGNALS
            else:
                daily_limit = get_max_daily_signals(market_state)
            a_candidates.sort(key=lambda x: -x["total_score"])
            for cand in a_candidates[:daily_limit]:
                signals_a.append({
                    "date": date_str, "code": cand["code"],
                    "name": cand["name"], "score_ml": cand["ml_score"] or 0,
                    "total_score": cand["total_score"],
                    "kdj_pass": cand["kdj_pass"],
                    "market_state": market_state_str,
                })

        # ═══ 策略B: 纯ML Top 5 ═══
        # 候选池所有股票按ML评分排序
        b_sorted = sorted(
            [(c, s) for c, s in code_ml_scores.items() if s is not None],
            key=lambda x: -x[1]
        )
        # 同时要求至少ML ≥ 10（和当前门槛一致）
        b_filtered = [(c, s) for c, s in b_sorted if s >= ML_SCORE_MIN_THRESHOLD]
        if not b_filtered:
            b_filtered = b_sorted[:MAX_DAILY_ML]  # 如果都不满足门槛，取最高的

        for code, ml_val in b_filtered[:MAX_DAILY_ML]:
            signals_b.append({
                "date": date_str, "code": code,
                "name": stock_name_map.get(code, ""),
                "score_ml": ml_val,
                "total_score": 0,
                "kdj_pass": False,
                "market_state": market_state_str,
            })

        # ═══ 策略C: ML + 动态持有期 ═══
        # 和B用同样的排序，但simulate时传dynamic_hold=True
        for code, ml_val in b_filtered[:MAX_DAILY_ML]:
            signals_c.append({
                "date": date_str, "code": code,
                "name": stock_name_map.get(code, ""),
                "score_ml": ml_val,
                "total_score": 0,
                "kdj_pass": False,
                "market_state": market_state_str,
            })

    # ── 构建三组信号DataFrame ──
    df_a = pd.DataFrame(signals_a) if signals_a else pd.DataFrame()
    df_b = pd.DataFrame(signals_b) if signals_b else pd.DataFrame()
    df_c = pd.DataFrame(signals_c) if signals_c else pd.DataFrame()

    logger.info("信号数: A=%d, B=%d, C=%d", len(df_a), len(df_b), len(df_c))

    # ── 保存信号CSV ──
    for name, df_sig in [("A_current", df_a), ("B_pure_ml", df_b), ("C_ml_dynamic", df_c)]:
        if not df_sig.empty:
            csv_path = os.path.join(OUTPUT_PATH, f"backtest_{name}_{start_date}_{end_date}.csv")
            df_sig.to_csv(csv_path, index=False, encoding="utf-8-sig")
            logger.info("保存 %s 信号: %s", name, csv_path)

    # ── 组合模拟 ──
    # 策略A: V1.0纯信号动态持有（ML评分动态持有期 + 市场状态感知）
    logger.info("运行策略A组合模拟 (V1.0纯信号动态持有)...")
    result_a = simulate_pure_portfolio(
        df_a[["date", "code", "name", "score_ml"]],
        stock_daily, trading_dates=trading_dates,
        dynamic_hold=True,
        use_price_stop=True,
        use_partial_take_profit=True,
        use_trailing_stop=True,
    ) if not df_a.empty else {"total_trades": 0, "final_value": 1_000_000, "total_return": 0}

    # 策略B: 纯ML评分 (固定持有5天)
    logger.info("运行策略B组合模拟 (纯ML评分, 固定持有5天)...")
    result_b = simulate_pure_portfolio(
        df_b[["date", "code", "name", "score_ml"]],
        stock_daily, trading_dates=trading_dates,
        dynamic_hold=False, hold_days=5,
    ) if not df_b.empty else {"total_trades": 0, "final_value": 1_000_000, "total_return": 0}

    # 策略C: ML评分+动态持有期(按评分3/5/7天)
    logger.info("运行策略C组合模拟 (ML+动态持有期)...")
    result_c = simulate_pure_portfolio(
        df_c[["date", "code", "name", "score_ml"]],
        stock_daily, trading_dates=trading_dates,
        dynamic_hold=True,
    ) if not df_c.empty else {"total_trades": 0, "final_value": 1_000_000, "total_return": 0}

    # ── 打印结果 ──
    print("\n" + "=" * 90)
    print("  【三策略对比回测结果】")
    print(f"  回测区间: {start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} ~ "
          f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}")
    print("=" * 90)
    print(f"  {'指标':<18} {'A·V1.0纯信号':>18} {'B·纯ML评分':>18} {'C·ML+动态':>18}")
    print(f"  {'─'*16:<18} {'─'*16:>18} {'─'*16:>18} {'─'*16:>18}")
    print(f"  {'信号总数':<18} {len(df_a):>18} {len(df_b):>18} {len(df_c):>18}")
    print(f"  {'总收益率%':<18} {result_a.get('total_return',0):>+18.2f} {result_b.get('total_return',0):>+18.2f} {result_c.get('total_return',0):>+18.2f}")
    print(f"  {'年化收益%':<18} {result_a.get('ann_return',0):>+18.2f} {result_b.get('ann_return',0):>+18.2f} {result_c.get('ann_return',0):>+18.2f}")
    print(f"  {'最大回撤%':<18} {result_a.get('max_drawdown',0):>18.2f} {result_b.get('max_drawdown',0):>18.2f} {result_c.get('max_drawdown',0):>18.2f}")
    print(f"  {'夏普比率':<18} {result_a.get('sharpe',0):>18.2f} {result_b.get('sharpe',0):>18.2f} {result_c.get('sharpe',0):>18.2f}")
    print(f"  {'胜率%':<18} {result_a.get('win_rate',0):>18.1f} {result_b.get('win_rate',0):>18.1f} {result_c.get('win_rate',0):>18.1f}")
    print(f"  {'交易次数':<18} {result_a.get('total_trades',0):>18} {result_b.get('total_trades',0):>18} {result_c.get('total_trades',0):>18}")
    print(f"  {'盈亏比':<18} {result_a.get('profit_ratio',0):>18.2f} {result_b.get('profit_ratio',0):>18.2f} {result_c.get('profit_ratio',0):>18.2f}")
    print(f"  {'Profit Factor':<18} {result_a.get('profit_factor',0):>18.2f} {result_b.get('profit_factor',0):>18.2f} {result_c.get('profit_factor',0):>18.2f}")
    print("=" * 90)

    # ── 策略对比解读 ──
    print("\n【策略说明】")
    print("  A·V1.0:  RPS板块过滤 → MACD∩ZJTJ核心 → ML评分动态持有(7/5/3天) → 市场状态自适应")
    print("  B·纯ML评分: RPS板块过滤 → ML评分Top5买入 → 固定持有5天")
    print("  C·ML+动态:  RPS板块过滤 → ML评分Top5买入 → 按评分动态持有(3/5/7天)")

    # ML评分分布统计
    all_ml = []
    for sig in signals_b:
        if sig["score_ml"] is not None:
            all_ml.append(sig["score_ml"])
    if all_ml:
        arr = np.array(all_ml)
        print(f"\n【纯ML评分分布 (B/C策略)】")
        print(f"  总数={len(arr)}, 均值={arr.mean():.1f}, 中位={np.median(arr):.1f}")
        for lo in range(0, 16, 3):
            hi = min(lo + 2, 15)
            cnt = np.sum((arr >= lo) & (arr <= hi))
            if cnt > 0:
                print(f"  评分[{lo}-{hi}]: {cnt}次 ({cnt/len(arr)*100:.1f}%)")

    return df_a, df_b, df_c, result_a, result_b, result_c


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    run_comparison(quick=quick)
