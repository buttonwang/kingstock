"""66大顺 V2.2 纸上交易 — 每日执行脚本

每个交易日下午5点后运行（main.py之后）:
  1. 执行昨日挂单（以今日开盘价买入）
  2. 检查持仓退出（以今日收盘价判断）
  3. 生成今日V2.2信号（挂单，明日开盘执行）
  4. 记录当日净值
  5. 打印日报

用法:
    python scripts/run_paper_trade.py                  # 今日
    python scripts/run_paper_trade.py --date 20260609  # 指定日期
    python scripts/run_paper_trade.py --reset          # 重置账本
"""

import sys
import os
import argparse
import json
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.market_state import get_market_state, is_tradeable
from src.email_reporter import send_email, is_email_configured
from config.settings import RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.indicators.macd import calculate_macd, is_macd_buy_signal
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.scoring import compute_total_score
from src.ml import ml_scorer
from scripts.v2_0_execution import V2_0_CONFIG
from scripts.paper_trader import PaperTrader


def parse_args():
    parser = argparse.ArgumentParser(description="66大顺 V2.2 纸上交易每日执行")
    parser.add_argument("--date", type=str, default=None, help="执行日期 YYYYMMDD")
    parser.add_argument("--reset", action="store_true", help="重置账本")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始资金")
    return parser.parse_args()


def check_weekly_trend(df):
    """周线MACD多头确认（与回测一致）"""
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


def get_open_prices(fetcher, codes, date_str):
    """获取指定日期的开盘价"""
    fmt_date = pd.Timestamp(date_str).strftime("%Y-%m-%d")
    prices = {}
    for code in codes:
        try:
            df = fetcher._sql_to_df(
                "SELECT open FROM stock_daily WHERE code=? AND date=?",
                params=(code, fmt_date),
            )
            if not df.empty:
                prices[code] = float(df.iloc[0]["open"])
        except Exception:
            pass
    return prices


def get_close_prices(fetcher, codes, date_str):
    """获取指定日期的收盘价"""
    fmt_date = pd.Timestamp(date_str).strftime("%Y-%m-%d")
    prices = {}
    for code in codes:
        try:
            df = fetcher._sql_to_df(
                "SELECT close FROM stock_daily WHERE code=? AND date=?",
                params=(code, fmt_date),
            )
            if not df.empty:
                prices[code] = float(df.iloc[0]["close"])
        except Exception:
            pass
    return prices


def get_trading_dates(fetcher, date_str):
    """获取最近的交易日列表（用于RPS计算）"""
    fmt_end = pd.Timestamp(date_str).strftime("%Y-%m-%d")
    df = fetcher._sql_to_df(
        "SELECT DISTINCT date FROM sector_daily WHERE date<=? ORDER BY date DESC LIMIT 30",
        params=(fmt_end,),
    )
    if df.empty:
        return []
    dates = sorted(df["date"].tolist())
    return [str(d)[:10] for d in dates]


def find_prev_trading_date(fetcher, date_str):
    """找到当前日期之前的最近交易日"""
    fmt_date = pd.Timestamp(date_str).strftime("%Y-%m-%d")
    df = fetcher._sql_to_df(
        "SELECT DISTINCT date FROM sector_daily WHERE date<? ORDER BY date DESC LIMIT 1",
        params=(fmt_date,),
    )
    if df.empty:
        return None
    return str(df.iloc[0]["date"])[:10]


def generate_v22_signals(fetcher, date_str):
    """生成V2.2双轨道信号（与回测逻辑一致）"""
    fmt_date = pd.Timestamp(date_str)
    fmt_start = (fmt_date - pd.Timedelta(days=300)).strftime("%Y-%m-%d")
    fmt_end = fmt_date.strftime("%Y-%m-%d")

    # 加载板块数据
    sec_df = fetcher._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
    sector_daily = {}
    for _, row in sec_df.iterrows():
        df = fetcher._sql_to_df(
            "SELECT date, close, change_pct FROM sector_daily "
            "WHERE sector_name=? AND sector_type=? AND date>=? AND date<=? ORDER BY date",
            params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
        )
        if not df.empty:
            sector_daily[row["sector_name"]] = df

    # 加载板块成分
    cons_df = fetcher._sql_to_df("SELECT sector_name, code, name FROM sector_constituents")
    sector_constituents = {}
    stock_name_map = {}
    for _, row in cons_df.iterrows():
        n = row["sector_name"]
        sector_constituents.setdefault(n, set()).add(row["code"])
        if row["name"]:  # sector_constituents 的 name 字段可能为空
            stock_name_map[row["code"]] = row["name"]
    
    # 补充 stock_list 中的名称（覆盖空值）
    try:
        name_df = fetcher._sql_to_df("SELECT code, name FROM stock_list")
        for _, row in name_df.iterrows():
            if row["name"] and (row["code"] not in stock_name_map or not stock_name_map[row["code"]]):
                stock_name_map[row["code"]] = row["name"]
    except Exception:
        pass

    # RPS计算
    sdata = {}
    for name, df in sector_daily.items():
        sub = df[pd.to_datetime(df["date"]) <= fmt_date]
        if len(sub) >= RPS_PERIOD:
            sdata[name] = sub
    if not sdata:
        print("  RPS数据不足，跳过信号生成")
        return [], "weak"

    try:
        rps_df = calculate_sector_rps(sdata, period=RPS_PERIOD)
        top_sectors = get_top_sectors(rps_df, top_n=RPS_TOP_N_STRICT)
    except Exception:
        print("  RPS计算失败，跳过信号生成")
        return [], "weak"

    if rps_df.empty:
        return [], "weak"

    rps_rank_map = dict(zip(rps_df["sector_name"], rps_df["rps_rank"]))
    code_to_sector = {}
    for name in top_sectors:
        for c in sector_constituents.get(name, set()):
            code_to_sector.setdefault(c, name)

    # 加载个股数据
    stock_dict = {}
    for code in code_to_sector:
        df = fetcher._sql_to_df(
            "SELECT date, open, high, low, close, volume, turnover_rate "
            "FROM stock_daily WHERE code=? AND date>=? AND date<=? ORDER BY date",
            params=(code, fmt_start, fmt_end),
        )
        if not df.empty and len(df) >= 60:
            stock_dict[code] = df
    if not stock_dict:
        return [], "weak"

    # 市场状态
    past_returns = []
    for code, df in stock_dict.items():
        closes = df["close"].values
        if len(closes) >= 11:
            past_returns.append((closes[-1] / closes[-11] - 1) * 100)
    market_10d = np.mean(past_returns) if past_returns else 0
    market_state = get_market_state(market_10d)

    if not is_tradeable(market_state):
        print(f"  市场状态: {market_state} (10d均值={market_10d:.2f}%) → 不交易")
        return [], "weak"

    print(f"  市场状态: {market_state} (10d均值={market_10d:.2f}%)")

    # 筛选
    macd_codes = filter_by_macd(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)

    ml_avail = ml_scorer.is_available()
    TA_ML_MIN = V2_0_CONFIG['track_a']['ml_min']
    TA_ENH_MIN = V2_0_CONFIG['track_a']['enhanced_rules_min']
    TB_ML_MIN = V2_0_CONFIG['track_b']['ml_min']
    TB_ENH_MIN = V2_0_CONFIG['track_b']['enhanced_rules_min']

    signals = []
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

        # MACD
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

        # 周线
        weekly_ok = check_weekly_trend(df)

        in_zjtj = code in zjtj_codes
        score_ml = int(round(ml_val)) if ml_val is not None else 0

        # Track A: V1.0 核心
        if (macd_strict and in_zjtj and weekly_ok
                and rules_passed >= TA_ENH_MIN
                and (ml_val is not None and ml_val >= TA_ML_MIN)):
            try:
                dk = calculate_kdj(df)
                dz = calculate_zjtj(df)
                scores = compute_total_score(df_macd, dk, dz, rps_rank, ml_score=ml_val)
            except Exception:
                continue
            signals.append({
                "code": code,
                "name": stock_name_map.get(code, ""),
                "signal_track": 0,
                "score_ml": score_ml,
                "total_score": scores.get("total_score", 0),
                "market_state": market_state,
            })
            continue  # 已分配到A，跳过B

        # Track B: V1.1 扩容（ZJTJ-only, RPS≤5）
        if (in_zjtj and weekly_ok
                and rules_passed >= TB_ENH_MIN
                and (ml_val is not None and ml_val >= TB_ML_MIN)
                and rps_rank <= 5):
            try:
                dk = calculate_kdj(df)
                dz = calculate_zjtj(df)
                scores = compute_total_score(df_macd, dk, dz, rps_rank, ml_score=ml_val)
            except Exception:
                continue
            signals.append({
                "code": code,
                "name": stock_name_map.get(code, ""),
                "signal_track": 1,
                "score_ml": score_ml,
                "total_score": scores.get("total_score", 0),
                "market_state": market_state,
            })

    return signals, market_state


def main():
    args = parse_args()

    # 确定日期
    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now().strftime("%Y%m%d")

    fmt_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    print("=" * 70)
    print(f"66大顺 V2.2 纸上交易 — {fmt_date}")
    print("=" * 70)

    # 重置账本
    if args.reset:
        state_file = os.path.join(BASE_DIR, "data", "output", "paper_trade_state.json")
        if os.path.exists(state_file):
            os.remove(state_file)
            print("已删除旧账本")

    # 初始化
    trader = PaperTrader(initial_capital=args.capital)

    # 连接数据库
    fetcher = DataFetcher()

    # 验证日期是否是交易日（优先sector_daily，备选stock_daily）
    check_date = pd.Timestamp(date_str).strftime("%Y-%m-%d")
    check = fetcher._sql_to_df(
        "SELECT COUNT(*) as cnt FROM sector_daily WHERE date=?",
        params=(check_date,),
    )
    is_trading_day = (not check.empty and check.iloc[0]["cnt"] > 0)
    if not is_trading_day:
        # 备选：用stock_daily判断
        check2 = fetcher._sql_to_df(
            "SELECT COUNT(*) as cnt FROM stock_daily WHERE date=?",
            params=(check_date,),
        )
        is_trading_day = (not check2.empty and check2.iloc[0]["cnt"] > 0)
    if not is_trading_day:
        print(f"  {fmt_date} 不是交易日，退出")
        fetcher.close()
        return

    # ── Step 1: 执行昨日挂单（以今日开盘价，含涨停检测） ──
    pending = trader.state.get("pending_orders", [])
    if pending:
        print(f"\n[Step 1] 执行挂单: {len(pending)}笔（以今日开盘价买入）")
        pending_codes = [o["code"] for o in pending]
        open_prices = get_open_prices(fetcher, pending_codes, date_str)

        # 获取信号日收盘价（用于涨停检测）
        close_prices = {}
        signal_dates = set(o["signal_date"] for o in pending)
        for sd in signal_dates:
            sd_prices = get_close_prices(fetcher, pending_codes, sd)
            close_prices.update(sd_prices)

        if open_prices:
            executed = trader.execute_pending_orders(
                open_prices, date_str, close_price_map=close_prices
            )
            print(f"  执行了{executed}笔买入")
        else:
            print("  无法获取开盘价，挂单保留")
    else:
        print(f"\n[Step 1] 无挂单")

    # ── Step 2: 检查持仓退出（以今日收盘价） ──
    positions = trader.state.get("positions", {})
    if positions:
        print(f"\n[Step 2] 检查持仓退出: {len(positions)}只")
        pos_codes = list(positions.keys())
        close_prices = get_close_prices(fetcher, pos_codes, date_str)

        if close_prices:
            sells = trader.check_exits(close_prices, date_str)
            if sells:
                trader.execute_sells(sells, date_str)
            else:
                print("  无退出信号")
        else:
            print("  无法获取收盘价")
    else:
        print(f"\n[Step 2] 无持仓")

    # ── Step 3: 生成今日V2.2信号 ──
    print(f"\n[Step 3] 生成V2.2信号...")
    signals, market_state = generate_v22_signals(fetcher, date_str)

    if signals:
        track_a = sum(1 for s in signals if s["signal_track"] == 0)
        track_b = sum(1 for s in signals if s["signal_track"] == 1)
        print(f"  信号数: {len(signals)} (Track A: {track_a}, Track B: {track_b})")
        for s in signals:
            track_name = "A" if s["signal_track"] == 0 else "B"
            print(f"    [{track_name}] {s['code']} {s['name']} ML={s['score_ml']}")

        # 挂单（明日执行）
        trader.generate_orders(signals, date_str, market_state)
    else:
        print("  今日无信号")

    # ── Step 4: 记录当日净值 ──
    print(f"\n[Step 4] 记录净值")
    all_codes = list(trader.state["positions"].keys())
    if all_codes:
        close_prices = get_close_prices(fetcher, all_codes, date_str)
    else:
        close_prices = {}

    nav_record = trader.record_daily_nav(date_str, close_prices)
    print(f"  总资产: ¥{nav_record['total_value']:,.2f}")
    print(f"  净值: {nav_record['nav']:.4f}")
    print(f"  持仓: {nav_record['positions_count']}只")

    # ── 补全持仓名称（修复历史空名称）──
    for code, pos in trader.state.get("positions", {}).items():
        if not pos.get("name") and code in stock_name_map:
            pos["name"] = stock_name_map[code]
    for order in trader.state.get("pending_orders", []):
        if not order.get("name") and order["code"] in stock_name_map:
            order["name"] = stock_name_map[order["code"]]

    # ── 保存 ──
    trader.save()
    trader.append_trade_log_csv()

    # ── 打印状态 ──
    trader.print_status(close_prices)

    # ── 打印累计统计 ──
    summary = trader.get_summary()
    if summary["total_trades"] > 0:
        print(f"\n{'='*60}")
        print(f"累计统计 ({summary['trading_days']}个交易日)")
        print(f"{'='*60}")
        print(f"  总交易: {summary['total_trades']}笔")
        print(f"  胜率:   {summary['win_rate']:.1f}%")
        print(f"  平均回报: {summary['avg_return']:.2f}%")
        print(f"  总盈亏: ¥{summary['total_pnl']:+,.2f}")
        print(f"  交易成本: ¥{summary['total_commission']:,.2f}")
        print(f"  最大回撤: {summary['max_drawdown']:.2f}%")

        track_names = {0: "A(核心)", 1: "B(扩容)"}
        for tid, stats in summary.get("track_stats", {}).items():
            print(f"  {track_names.get(tid, f'Track{tid}')}: "
                  f"{stats['count']}笔, 胜率{stats['win_rate']:.1f}%, "
                  f"平均{stats['avg_return']:.2f}%, "
                  f"盈亏¥{stats['total_pnl']:+,.0f}")

    fetcher.close()

    # ── Step 5: 每日交易日报 + 邮件发送 ──
    report = trader.build_daily_report(date_str)
    print(report)

    # 发送邮件
    if is_email_configured():
        display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        subject = f"[纸上交易] {display_date} 日报"
        try:
            send_email(subject, report)
            print("[PaperTrader] 日报邮件已发送")
        except Exception as e:
            print(f"[PaperTrader] 邮件发送失败: {e}")
    else:
        print("[PaperTrader] 邮件未配置，跳过发送")

    print(f"\n纸上交易日报完成 — {fmt_date}")


if __name__ == "__main__":
    main()
