"""66大顺 V2.2 实盘交易 — 每日执行脚本 (QMT)

每个交易日分两次运行:

【盘后 15:30+】生成信号 + 挂单
    python scripts/run_live_trade.py --phase signal

【盘前 09:26+】执行挂单 + 检查退出
    python scripts/run_live_trade.py --phase execute

【合并模式】收盘后一次性运行（适合手动操作）
    python scripts/run_live_trade.py
    python scripts/run_live_trade.py --date 20260609

运行模式:
    config/qmt_config.py 中 DRY_RUN=True  → 模拟模式（不实际下单）
    config/qmt_config.py 中 DRY_RUN=False → 实盘模式（真实下单）

Version: 1.0
Date: 2026-06-09
"""

import sys
import os
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.qmt_trader import QmtTrader
from config.qmt_config import DRY_RUN, NOTIFY_DAILY_REPORT

# 复用纸上交易中的信号生成和数据工具函数
from scripts.run_paper_trade import (
    generate_v22_signals,
    get_open_prices,
    get_close_prices,
    check_weekly_trend,
)


def parse_args():
    parser = argparse.ArgumentParser(description="66大顺 V2.2 实盘交易 (QMT)")
    parser.add_argument("--date", type=str, default=None,
                        help="执行日期 YYYYMMDD（默认今日）")
    parser.add_argument("--phase", type=str, default="all",
                        choices=["signal", "execute", "all"],
                        help="执行阶段: signal=盘后信号, execute=盘前下单, all=合并")
    parser.add_argument("--reset", action="store_true",
                        help="重置状态文件（谨慎使用）")
    return parser.parse_args()


def main():
    args = parse_args()

    # 确定日期
    if args.date:
        date_str = args.date
    else:
        date_str = datetime.now().strftime("%Y%m%d")

    fmt_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    mode = "DRY_RUN 模拟" if DRY_RUN else "⚡ 实盘"

    print("=" * 70)
    print(f"66大顺 V2.2 实盘交易 — {fmt_date}  [{mode}]")
    print("=" * 70)

    # 重置
    if args.reset:
        from config.qmt_config import LIVE_STATE_FILE
        if os.path.exists(LIVE_STATE_FILE):
            os.remove(LIVE_STATE_FILE)
            print("⚠️ 已删除实盘状态文件")

    # 初始化交易引擎
    trader = QmtTrader()

    # 连接 QMT
    if not trader.connect():
        print("[错误] QMT 连接失败，请检查 miniQMT 客户端是否已启动")
        return

    # 连接数据库
    fetcher = DataFetcher()

    # 验证交易日
    check_date = pd.Timestamp(date_str).strftime("%Y-%m-%d")
    check = fetcher._sql_to_df(
        "SELECT COUNT(*) as cnt FROM stock_daily WHERE date=?",
        params=(check_date,),
    )
    is_trading_day = (not check.empty and check.iloc[0]["cnt"] > 0)
    if not is_trading_day:
        print(f"  {fmt_date} 不是交易日（无行情数据），退出")
        fetcher.close()
        trader.disconnect()
        return

    # 同步券商持仓
    trader.sync_with_broker()

    phase = args.phase

    # ════════════════════════════════════════════════════════
    # Phase 1: 执行挂单（盘前 09:26 或合并模式）
    # ════════════════════════════════════════════════════════
    if phase in ("execute", "all"):
        pending = trader.state.get("pending_orders", [])
        if pending:
            print(f"\n[Step 1] 执行挂单: {len(pending)}笔")
            pending_codes = [o["code"] for o in pending]
            open_prices = get_open_prices(fetcher, pending_codes, date_str)

            # 信号日收盘价（涨停检测）
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

        # 检查持仓退出
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
                print("  无法获取收盘价，跳过退出检查")
        else:
            print(f"\n[Step 2] 无持仓")

    # ════════════════════════════════════════════════════════
    # Phase 2: 生成信号（盘后 15:30 或合并模式）
    # ════════════════════════════════════════════════════════
    if phase in ("signal", "all"):
        print(f"\n[Step 3] 生成V2.2信号...")
        signals, market_state = generate_v22_signals(fetcher, date_str)

        if signals:
            track_a = sum(1 for s in signals if s["signal_track"] == 0)
            track_b = sum(1 for s in signals if s["signal_track"] == 1)
            print(f"  信号数: {len(signals)} (Track A: {track_a}, Track B: {track_b})")
            for s in signals:
                track_name = "A" if s["signal_track"] == 0 else "B"
                print(f"    [{track_name}] {s['code']} {s['name']} ML={s['score_ml']}")

            trader.generate_orders(signals, date_str, market_state)
        else:
            print("  今日无信号")

    # ════════════════════════════════════════════════════════
    # Step 4: 记录净值 + 保存 + 日报
    # ════════════════════════════════════════════════════════
    print(f"\n[Step 4] 记录净值")
    all_codes = list(trader.state.get("positions", {}).keys())
    if all_codes:
        close_prices = get_close_prices(fetcher, all_codes, date_str)
    else:
        close_prices = {}

    nav_record = trader.record_daily_nav(date_str, close_prices)
    print(f"  总资产: ¥{nav_record['total_value']:,.2f}")
    print(f"  净值: {nav_record['nav']:.4f}")
    print(f"  持仓: {nav_record['positions_count']}只")

    # 保存
    trader.save()
    trader.append_trade_log_csv()

    # 打印状态
    trader.print_status(close_prices)

    # 日报
    report = trader.build_daily_report(date_str)
    print(report)

    # 微信推送
    if NOTIFY_DAILY_REPORT:
        try:
            from src.wechat_pusher import push_message
            push_message("66大顺实盘日报", report)
            print("[QmtTrader] 微信推送已发送")
        except Exception as e:
            print(f"[QmtTrader] 微信推送失败: {e}")

    # 邮件
    try:
        from src.email_reporter import send_email, is_email_configured
        if is_email_configured():
            subject = f"[{'模拟' if DRY_RUN else '实盘'}] {fmt_date} 日报"
            send_email(subject, report)
            print("[QmtTrader] 日报邮件已发送")
    except Exception as e:
        print(f"[QmtTrader] 邮件发送失败: {e}")

    # 断开连接
    trader.disconnect()
    fetcher.close()

    print(f"\n实盘交易执行完成 — {fmt_date}")


if __name__ == "__main__":
    main()
