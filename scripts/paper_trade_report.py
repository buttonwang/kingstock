"""66大顺 V2.2 纸上交易 — 周度复盘报告

生成每周复盘报告，包括：
  - 本周收益/回撤/交易明细
  - 与回测基准对比
  - 轨道A/B分项统计
  - 交易成本分析
  - 净值曲线图

用法:
    python scripts/paper_trade_report.py                   # 完整报告
    python scripts/paper_trade_report.py --week 20260602   # 指定周起始日
"""

import sys
import os
import argparse
import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from scripts.paper_trader import STATE_FILE, TRADE_LOG_FILE

# V2.2 回测基准（3年数据）
V22_BENCHMARK = {
    "ann_return": 19.80,
    "max_drawdown": 10.61,
    "sharpe": 1.40,
    "win_rate": 46.0,
    "avg_return_per_trade": 1.65,
    "trades_per_week": 290 / 3 / 52 * 5,  # ~9.3
}


def parse_args():
    parser = argparse.ArgumentParser(description="66大顺 V2.2 纸上交易周度复盘")
    parser.add_argument("--week", type=str, default=None, help="周起始日 YYYYMMDD")
    return parser.parse_args()


def load_state():
    """加载纸上交易状态"""
    if not os.path.exists(STATE_FILE):
        print("未找到纸上交易状态文件，请先运行 run_paper_trade.py")
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_trade_log():
    """加载交易日志CSV"""
    if not os.path.exists(TRADE_LOG_FILE):
        return pd.DataFrame()
    return pd.read_csv(TRADE_LOG_FILE, encoding="utf-8-sig")


def analyze_week(state, trades_df, week_start=None):
    """分析指定周的表现"""
    nav_history = state.get("daily_nav", [])
    if not nav_history:
        return None

    nav_df = pd.DataFrame(nav_history)
    nav_df["date"] = nav_df["date"].astype(str)

    if week_start:
        week_end = (pd.Timestamp(week_start) + timedelta(days=6)).strftime("%Y%m%d")
        week_nav = nav_df[(nav_df["date"] >= week_start) & (nav_df["date"] <= week_end)]
        week_trades = trades_df[
            (trades_df["exit_date"].astype(str) >= week_start)
            & (trades_df["exit_date"].astype(str) <= week_end)
        ] if not trades_df.empty else pd.DataFrame()
    else:
        # 最近一周
        last_date = nav_df["date"].iloc[-1]
        week_ago = (pd.Timestamp(last_date) - timedelta(days=6)).strftime("%Y%m%d")
        week_nav = nav_df[nav_df["date"] >= week_ago]
        week_trades = trades_df[
            trades_df["exit_date"].astype(str) >= week_ago
        ] if not trades_df.empty else pd.DataFrame()

    if week_nav.empty:
        return None

    week_return = (week_nav.iloc[-1]["nav"] / week_nav.iloc[0]["nav"] - 1) * 100
    week_max_dd = 0
    peak = week_nav.iloc[0]["total_value"]
    for _, row in week_nav.iterrows():
        if row["total_value"] > peak:
            peak = row["total_value"]
        dd = (peak - row["total_value"]) / peak * 100
        if dd > week_max_dd:
            week_max_dd = dd

    return {
        "week_start": week_nav.iloc[0]["date"],
        "week_end": week_nav.iloc[-1]["date"],
        "trading_days": len(week_nav),
        "week_return_pct": round(week_return, 2),
        "week_max_dd": round(week_max_dd, 2),
        "start_value": round(week_nav.iloc[0]["total_value"], 2),
        "end_value": round(week_nav.iloc[-1]["total_value"], 2),
        "trades_count": len(week_trades),
        "trades": week_trades,
    }


def generate_chart(state, output_path):
    """生成净值曲线图"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter, DayLocator

    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

    nav_history = state.get("daily_nav", [])
    if len(nav_history) < 2:
        return None

    nav_df = pd.DataFrame(nav_history)
    nav_df["date"] = pd.to_datetime(nav_df["date"])
    nav_df = nav_df.sort_values("date")

    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle("66大顺 V2.2 纸上交易 — 净值曲线", fontsize=16, fontweight="bold")

    # 1) 净值曲线
    ax1 = axes[0]
    ax1.plot(nav_df["date"], nav_df["nav"], color="#0066CC", linewidth=2, label="纸上交易净值")
    ax1.axhline(y=1.0, color="gray", linewidth=0.5, linestyle="--")
    ax1.set_ylabel("净值")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)
    ax1.set_title("净值走势", fontsize=13)

    # 2) 回撤
    ax2 = axes[1]
    peak = nav_df["nav"].cummax()
    dd = (nav_df["nav"] - peak) / peak * 100
    ax2.fill_between(nav_df["date"], dd.values, 0, color="#CC0000", alpha=0.4, label="回撤")
    ax2.set_ylabel("回撤 (%)")
    ax2.legend(loc="lower left")
    ax2.grid(True, alpha=0.3)
    ax2.set_title("回撤走势", fontsize=13)

    # 3) 持仓数量
    ax3 = axes[2]
    ax3.bar(nav_df["date"], nav_df["positions_count"], color="#0066CC", alpha=0.6, width=0.8)
    ax3.set_ylabel("持仓数量")
    ax3.set_title("每日持仓数量", fontsize=13)
    ax3.grid(True, alpha=0.3)

    ax3.xaxis.set_major_locator(DayLocator(interval=2))
    ax3.xaxis.set_major_formatter(DateFormatter('%m-%d'))
    plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=9)

    plt.tight_layout()
    fig.subplots_adjust(bottom=0.1)
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    return output_path


def main():
    args = parse_args()

    state = load_state()
    if state is None:
        return

    trades_df = load_trade_log()

    print("=" * 70)
    print("66大顺 V2.2 纸上交易 — 周度复盘报告")
    print("=" * 70)

    # ── 总体统计 ──
    nav_history = state.get("daily_nav", [])
    trade_history = state.get("trade_history", [])

    if not nav_history:
        print("无净值记录，请至少运行一个交易日")
        return

    initial = state.get("initial_capital", 1_000_000)
    last_nav = nav_history[-1]
    current_value = last_nav["total_value"]
    total_return = (current_value / initial - 1) * 100
    trading_days = len(nav_history)

    # 最大回撤
    max_dd = 0
    peak = initial
    for rec in nav_history:
        v = rec["total_value"]
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # 年化收益（简单折算）
    if trading_days > 1:
        ann_return = ((current_value / initial) ** (252 / trading_days) - 1) * 100
    else:
        ann_return = 0

    # 夏普比率
    if trading_days > 5:
        daily_returns = []
        for i in range(1, len(nav_history)):
            prev = nav_history[i-1]["nav"]
            curr = nav_history[i]["nav"]
            if prev > 0:
                daily_returns.append(curr / prev - 1)
        if daily_returns:
            avg_ret = np.mean(daily_returns)
            std_ret = np.std(daily_returns, ddof=1)
            sharpe = (avg_ret - 0.02/252) / std_ret * np.sqrt(252) if std_ret > 0 else 0
        else:
            sharpe = 0
    else:
        sharpe = 0

    print(f"\n{'─'*70}")
    print(f"总体表现 ({state.get('start_date', '?')} ~ {last_nav['date']})")
    print(f"{'─'*70}")
    print(f"  初始资金:     ¥{initial:>14,.2f}")
    print(f"  当前资产:     ¥{current_value:>14,.2f}")
    print(f"  总收益:       {(total_return):>+13.2f}%")
    print(f"  年化收益:     {ann_return:>14.2f}%")
    print(f"  最大回撤:     {max_dd:>14.2f}%")
    print(f"  夏普比率:     {sharpe:>14.2f}")
    print(f"  交易天数:     {trading_days:>14d}")
    print(f"  总交易:       {len(trade_history):>14d}笔")

    # ── 与回测基准对比 ──
    print(f"\n{'─'*70}")
    print(f"与V2.2回测基准对比")
    print(f"{'─'*70}")
    print(f"  {'指标':<16} {'纸上交易':<14} {'回测基准':<14} {'偏差':<14}")
    print(f"  {'-'*58}")

    # 年化对比
    diff_ann = ann_return - V22_BENCHMARK["ann_return"]
    print(f"  {'年化收益%':<16} {ann_return:<14.2f} {V22_BENCHMARK['ann_return']:<14.2f} {diff_ann:>+14.2f}")

    diff_dd = max_dd - V22_BENCHMARK["max_drawdown"]
    print(f"  {'最大回撤%':<16} {max_dd:<14.2f} {V22_BENCHMARK['max_drawdown']:<14.2f} {diff_dd:>+14.2f}")

    diff_sr = sharpe - V22_BENCHMARK["sharpe"]
    print(f"  {'夏普比率':<16} {sharpe:<14.2f} {V22_BENCHMARK['sharpe']:<14.2f} {diff_sr:>+14.2f}")

    if trade_history:
        wins = [t for t in trade_history if t["return_pct"] > 0]
        win_rate = len(wins) / len(trade_history) * 100
        avg_ret = np.mean([t["return_pct"] for t in trade_history])
        diff_wr = win_rate - V22_BENCHMARK["win_rate"]
        print(f"  {'胜率%':<16} {win_rate:<14.1f} {V22_BENCHMARK['win_rate']:<14.1f} {diff_wr:>+14.1f}")
        diff_ar = avg_ret - V22_BENCHMARK["avg_return_per_trade"]
        print(f"  {'平均回报%':<16} {avg_ret:<14.2f} {V22_BENCHMARK['avg_return_per_trade']:<14.2f} {diff_ar:>+14.2f}")
    else:
        win_rate = avg_ret = 0

    # ── 本周分析 ──
    week_data = analyze_week(state, trades_df, args.week)
    if week_data:
        print(f"\n{'─'*70}")
        print(f"本周复盘 ({week_data['week_start']} ~ {week_data['week_end']})")
        print(f"{'─'*70}")
        print(f"  本周收益:     {week_data['week_return_pct']:>+13.2f}%")
        print(f"  本周最大回撤: {week_data['week_max_dd']:>14.2f}%")
        print(f"  本周交易:     {week_data['trades_count']:>14d}笔")
        print(f"  起始资产:     ¥{week_data['start_value']:>14,.2f}")
        print(f"  结束资产:     ¥{week_data['end_value']:>14,.2f}")

        if not week_data["trades"].empty:
            print(f"\n  本周交易明细:")
            print(f"  {'代码':<8} {'名称':<8} {'轨道':<4} {'盈亏%':>8} {'PnL':>10} {'原因':<16}")
            print(f"  {'-'*58}")
            for _, t in week_data["trades"].iterrows():
                track = "A" if t["signal_track"] == 0 else "B"
                print(f"  {t['code']:<8} {t['name']:<8} {track:<4} "
                      f"{t['return_pct']:>+7.2f}% ¥{t['pnl']:>+9,.0f} {t['exit_reason']:<16}")

    # ── 当前持仓 ──
    positions = state.get("positions", {})
    if positions:
        print(f"\n{'─'*70}")
        print(f"当前持仓 ({len(positions)}只)")
        print(f"{'─'*70}")
        print(f"  {'代码':<8} {'名称':<8} {'轨道':<4} {'ML':<4} {'天数':<4} "
              f"{'成本':>8} {'市值':>10} {'盈亏%':>8}")
        print(f"  {'-'*60}")
        for code, pos in positions.items():
            price = pos.get("last_price", pos["entry_price"])
            ret = (price / pos["entry_price"] - 1) * 100
            mv = pos["shares"] * price
            track = "A" if pos.get("signal_track", 0) == 0 else "B"
            print(f"  {code:<8} {pos['name']:<8} {track:<4} "
                  f"{pos.get('score_ml',0):<4} {pos.get('held_days',0):<4} "
                  f"¥{pos['entry_price']:>7.2f} ¥{mv:>9,.0f} {ret:>+7.2f}%")

    # ── 交易成本分析 ──
    if trade_history:
        total_commission = sum(t.get("total_cost", 0) for t in trade_history)
        total_pnl_gross = sum(t["pnl"] + t.get("total_cost", 0) for t in trade_history)
        avg_cost_per_trade = total_commission / len(trade_history)

        print(f"\n{'─'*70}")
        print(f"交易成本分析")
        print(f"{'─'*70}")
        print(f"  总交易成本:   ¥{total_commission:>14,.2f}")
        print(f"  平均每笔:     ¥{avg_cost_per_trade:>14,.2f}")
        print(f"  成本占利润比: {total_commission/abs(total_pnl_gross)*100 if total_pnl_gross != 0 else 0:>13.1f}%")
        print(f"  毛利润:       ¥{total_pnl_gross:>14,.2f}")
        print(f"  净利润:       ¥{total_pnl_gross - total_commission:>14,.2f}")

    # ── 轨道分项 ──
    if trade_history:
        print(f"\n{'─'*70}")
        print(f"轨道分项统计")
        print(f"{'─'*70}")
        track_names = {0: "A(V1.0核心)", 1: "B(V1.1扩容)"}
        for track in [0, 1]:
            t_trades = [t for t in trade_history if t["signal_track"] == track]
            if not t_trades:
                print(f"  {track_names[track]}: 无交易")
                continue
            t_wins = [t for t in t_trades if t["return_pct"] > 0]
            t_losses = [t for t in t_trades if t["return_pct"] <= 0]
            print(f"  {track_names[track]}:")
            print(f"    交易数: {len(t_trades)}")
            print(f"    胜率:   {len(t_wins)/len(t_trades)*100:.1f}%")
            print(f"    平均回报: {np.mean([t['return_pct'] for t in t_trades]):.2f}%")
            if t_wins:
                print(f"    平均盈利: {np.mean([t['return_pct'] for t in t_wins]):+.2f}%")
            if t_losses:
                print(f"    平均亏损: {np.mean([t['return_pct'] for t in t_losses]):.2f}%")
            print(f"    总盈亏: ¥{sum(t['pnl'] for t in t_trades):+,.2f}")

            # 退出原因
            exit_map = {}
            for t in t_trades:
                r = t["exit_reason"]
                exit_map[r] = exit_map.get(r, 0) + 1
            reasons_str = ", ".join(f"{r}:{c}" for r, c in sorted(exit_map.items()))
            print(f"    退出原因: {reasons_str}")

    # ── 生成图表 ──
    chart_path = os.path.join("data", "output", "paper_trade_nav_curve.png")
    chart = generate_chart(state, chart_path)
    if chart:
        print(f"\n净值曲线图已保存: {chart_path}")

    # ── 实战损耗预估 ──
    if trade_history and trading_days >= 3:
        print(f"\n{'─'*70}")
        print(f"实战损耗分析（基于{trading_days}个交易日数据）")
        print(f"{'─'*70}")
        avg_slippage = 0.15  # 0.15% 单边
        total_trades = len(trade_history)
        est_slippage_cost = sum(
            t.get("entry_price", 0) * t.get("shares", 0) * avg_slippage / 100 * 2
            for t in trade_history
        )
        print(f"  已计入成本: ¥{total_commission:>10,.2f} (佣金+印花税+滑点)")
        print(f"  其中滑点估计: ¥{est_slippage_cost:>10,.2f}")
        print(f"  年化成本拖累: {total_commission/current_value/trading_days*252*100:.2f}%")

    print(f"\n{'='*70}")
    print("周度复盘报告完成")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
