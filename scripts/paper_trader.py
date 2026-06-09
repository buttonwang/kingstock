"""66大顺 V2.2 纸上交易核心引擎

功能：
  - 管理模拟账本（现金 + 持仓）
  - 信号日收盘后产生，次日开盘价+滑点执行
  - 真实交易成本：佣金 + 印花税 + 滑点
  - Track A/B 差异化退出（与回测一致）
  - 持久化状态到 JSON，交易日志到 CSV

交易成本模型：
  - 佣金：万2.5（往返）
  - 印花税：千1（卖出）
  - 滑点：买入 +0.15%，卖出 -0.15%（保守估计）

Version: 1.0
Date: 2026-06-07
"""

import json
import os
import csv
from datetime import datetime
from collections import defaultdict

import pandas as pd
import numpy as np

from scripts.v2_0_execution import (
    V2_0_CONFIG,
    check_track_b_exit,
    get_track_hold_days,
    get_track_position_pct,
    get_market_multiplier,
)

# ── 常量 ──
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(BASE_DIR, "data", "output", "paper_trade_state.json")
TRADE_LOG_FILE = os.path.join(BASE_DIR, "data", "output", "paper_trade_log.csv")

# 交易成本参数
COMMISSION_RATE = 0.00025       # 万2.5 单边
STAMP_DUTY_RATE = 0.001         # 千1 卖出
SLIPPAGE_BUY = 0.0015           # 买入滑点 0.15%
SLIPPAGE_SELL = 0.0015          # 卖出滑点 0.15%

# 每日信号上限
DAILY_LIMITS = {
    'strong': {'a': 4, 'b': 3},
    'choppy': {'a': 3, 'b': 2},
}

# 涨停检测（硬约束，涨停开盘无法买入）
LIMIT_UP_THRESHOLD = 0.095  # 开盘涨幅超9.5%视为涨停


def _round2(val):
    return round(val, 2)


def _round4(val):
    return round(val, 4)


class PaperTrader:
    """V2.2 纸上交易引擎"""

    def __init__(self, initial_capital=1_000_000, state_file=None):
        self.state_file = state_file or STATE_FILE
        self.initial_capital = initial_capital
        self.state = None
        self._load_or_init()

    def _load_or_init(self):
        """加载已有状态或初始化新账本"""
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.state = json.load(f)
            print(f"[PaperTrader] 加载状态: {self.state_file}")
            print(f"  现金: ¥{self.state['cash']:,.2f}")
            print(f"  持仓: {len(self.state['positions'])}只")
            print(f"  净值: ¥{self.get_total_value():,.2f}")
        else:
            self.state = {
                "initial_capital": self.initial_capital,
                "cash": self.initial_capital,
                "positions": {},
                "pending_orders": [],
                "trade_history": [],
                "daily_nav": [],
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_update": None,
                "start_date": None,
            }
            print(f"[PaperTrader] 初始化新账本: ¥{self.initial_capital:,.0f}")

    def save(self):
        """持久化状态"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def get_total_value(self, price_map=None):
        """计算总资产（现金 + 持仓市值）"""
        positions_value = 0
        for code, pos in self.state["positions"].items():
            price = pos.get("last_price", pos["entry_price"])
            if price_map and code in price_map:
                price = price_map[code]
            positions_value += pos["shares"] * price
        return self.state["cash"] + positions_value

    def get_nav(self, price_map=None):
        """计算当前净值"""
        return self.get_total_value(price_map) / self.initial_capital

    # ── 信号 → 挂单（当日收盘后） ──

    def generate_orders(self, signals, signal_date, market_state):
        """根据V2.2信号生成次日挂单

        signals: list of dict, 每个包含:
            - code, name, signal_track, score_ml, market_state
        signal_date: 信号产生日期 (YYYYMMDD)
        market_state: 市场状态字符串
        """
        if not signals:
            return 0

        # 按轨道分组
        track_a = sorted(
            [s for s in signals if s.get("signal_track") == 0],
            key=lambda x: x.get("score_ml", 0), reverse=True
        )
        track_b = sorted(
            [s for s in signals if s.get("signal_track") == 1],
            key=lambda x: x.get("score_ml", 0), reverse=True
        )

        # 每日上限
        limits = DAILY_LIMITS.get(market_state, {'a': 0, 'b': 0})
        track_a = track_a[:limits['a']]
        track_b = track_b[:limits['b']]

        orders = []
        for sig in track_a + track_b:
            code = sig["code"]
            # 已持仓或已挂单的不重复
            if code in self.state["positions"]:
                continue
            if any(o["code"] == code for o in self.state["pending_orders"]):
                continue

            track = sig.get("signal_track", 0)
            mult = get_market_multiplier(track, market_state)
            if mult <= 0:
                continue

            orders.append({
                "code": code,
                "name": sig.get("name", ""),
                "signal_date": signal_date,
                "signal_track": track,
                "score_ml": sig.get("score_ml", 0),
                "market_state": market_state,
                "mult": mult,
            })

        self.state["pending_orders"] = orders
        if orders:
            print(f"[PaperTrader] 生成{len(orders)}笔挂单 (信号日{signal_date}):")
            for o in orders:
                track_name = "A" if o["signal_track"] == 0 else "B"
                print(f"  [{track_name}] {o['code']} {o['name']} ML={o['score_ml']} "
                      f"市况={o['market_state']}")
        return len(orders)

    # ── 执行挂单（次日开盘） ──

    def execute_pending_orders(self, open_price_map, exec_date, close_price_map=None):
        """执行前一日的挂单，以次日开盘价+滑点买入

        open_price_map: {code: open_price} 次日开盘价
        exec_date: 执行日期
        close_price_map: {code: close_price} 信号日收盘价（用于涨停检测）
        """
        orders = self.state["pending_orders"]
        if not orders:
            return 0

        executed = 0
        skipped_limit = 0
        for order in orders:
            code = order["code"]
            open_price = open_price_map.get(code)
            if open_price is None or open_price <= 0:
                print(f"  [跳过] {code} {order['name']}: 无开盘价数据")
                continue

            # ── 涨停检测（硬约束，涨停开盘无法买入） ──
            if close_price_map and code in close_price_map:
                signal_close = close_price_map[code]
                if signal_close > 0:
                    gap_pct = (open_price / signal_close - 1)
                    if gap_pct >= LIMIT_UP_THRESHOLD:
                        print(f"  [跳过] {code} {order['name']}: "
                              f"涨停开盘 +{gap_pct*100:.1f}%，无法买入")
                        skipped_limit += 1
                        continue

            # 实际买入价 = 开盘价 * (1 + 滑点)
            buy_price = open_price * (1 + SLIPPAGE_BUY)

            # 仓位计算
            track = order["signal_track"]
            pos_pct = get_track_position_pct(track)
            total_value = self.get_total_value()
            pos_size = total_value * pos_pct * order["mult"]
            pos_size = min(pos_size, self.state["cash"] * 0.98)

            if pos_size < 1000:  # 太小不值得交易
                continue

            # 计算股数（A股100股整数）
            shares = int(pos_size / buy_price / 100) * 100
            if shares < 100:
                continue

            actual_cost = shares * buy_price
            commission = max(actual_cost * COMMISSION_RATE, 5)  # 最低5元

            total_cost = actual_cost + commission

            if total_cost > self.state["cash"]:
                # 资金不够，减少股数
                shares = int((self.state["cash"] - 5) / buy_price / 100) * 100
                if shares < 100:
                    continue
                actual_cost = shares * buy_price
                commission = max(actual_cost * COMMISSION_RATE, 5)
                total_cost = actual_cost + commission

            # 扣款
            self.state["cash"] -= total_cost

            # ML分档持有天数
            ml_score = order["score_ml"]
            hold_days = get_track_hold_days(ml_score, track)

            self.state["positions"][code] = {
                "name": order["name"],
                "entry_date": exec_date,
                "entry_price": _round4(buy_price),
                "shares": shares,
                "cost_basis": _round2(total_cost),
                "commission_buy": _round2(commission),
                "signal_track": track,
                "score_ml": ml_score,
                "hold_days_target": hold_days,
                "held_days": 0,
                "peak_price": _round4(buy_price),
                "signal_date": order["signal_date"],
                "market_state": order["market_state"],
            }
            executed += 1
            track_name = "A" if track == 0 else "B"
            print(f"  [买入] [{track_name}] {code} {order['name']} "
                  f"{shares}股 @ ¥{buy_price:.2f} = ¥{total_cost:,.0f} "
                  f"(佣金¥{commission:.0f}, 滑点+{SLIPPAGE_BUY*100:.2f}%)")

        self.state["pending_orders"] = []
        if skipped_limit:
            print(f"  [统计] 执行{executed}笔, 涨停跳过{skipped_limit}笔")
        return executed

    # ── 检查持仓退出 ──

    def check_exits(self, price_map, current_date, trading_date_idx=None):
        """检查所有持仓是否需要退出

        price_map: {code: close_price} 当日收盘价
        current_date: 当日日期
        trading_date_idx: 当前交易日序号（用于持有天数判断，可选）
        """
        to_sell = []

        for code, pos in list(self.state["positions"].items()):
            price = price_map.get(code)
            if price is None or price <= 0:
                continue

            # 更新持仓价格信息
            pos["last_price"] = _round4(price)
            pos["held_days"] += 1

            # 更新峰值
            if price > pos.get("peak_price", pos["entry_price"]):
                pos["peak_price"] = _round4(price)

            track = pos.get("signal_track", 0)
            exit_reason = None

            if track == 0:
                # Track A: Pure Hold — 到期卖出
                if pos["held_days"] >= pos.get("hold_days_target", 7):
                    exit_reason = "A_HOLD_EXPIRY"
            elif track == 1:
                # Track B: 动态退出
                exit_reason = check_track_b_exit(pos, price)
                if exit_reason is None:
                    # 也检查最大持有期
                    if pos["held_days"] >= V2_0_CONFIG["track_b"]["max_hold_days"]:
                        exit_reason = "B_HOLD_EXPIRY"

            if exit_reason:
                to_sell.append((code, pos, price, exit_reason))

        return to_sell

    def execute_sells(self, sells, current_date):
        """执行卖出

        sells: list of (code, pos, price, exit_reason)
        current_date: 当日日期
        """
        closed = []
        for code, pos, sell_price, reason in sells:
            # 实际卖出价 = 收盘价 * (1 - 滑点)
            actual_sell_price = sell_price * (1 - SLIPPAGE_SELL)
            shares = pos["shares"]
            gross_proceeds = shares * actual_sell_price

            # 交易成本
            commission = max(gross_proceeds * COMMISSION_RATE, 5)
            stamp_duty = gross_proceeds * STAMP_DUTY_RATE
            net_proceeds = gross_proceeds - commission - stamp_duty

            # 盈亏计算
            entry_price = pos["entry_price"]
            return_pct = (actual_sell_price / entry_price - 1) * 100
            pnl = net_proceeds - pos["cost_basis"]

            # 回款
            self.state["cash"] += net_proceeds

            track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
            print(f"  [卖出] [{track_name}] {code} {pos['name']} "
                  f"{shares}股 @ ¥{actual_sell_price:.2f} "
                  f"收益={return_pct:+.2f}% PnL=¥{pnl:+,.0f} "
                  f"({reason})")

            trade_record = {
                "code": code,
                "name": pos["name"],
                "signal_date": pos.get("signal_date", ""),
                "entry_date": pos["entry_date"],
                "exit_date": current_date,
                "entry_price": entry_price,
                "exit_price": _round4(actual_sell_price),
                "shares": shares,
                "signal_track": pos.get("signal_track", 0),
                "score_ml": pos.get("score_ml", 0),
                "return_pct": _round2(return_pct),
                "pnl": _round2(pnl),
                "exit_reason": reason,
                "held_days": pos.get("held_days", 0),
                "commission_buy": pos.get("commission_buy", 0),
                "commission_sell": _round2(commission),
                "stamp_duty": _round2(stamp_duty),
                "total_cost": _round2(pos.get("commission_buy", 0) + commission + stamp_duty),
            }
            closed.append(trade_record)
            self.state["trade_history"].append(trade_record)

            # 删除持仓
            del self.state["positions"][code]

        return closed

    # ── 每日收盘后记录净值 ──

    def record_daily_nav(self, current_date, price_map):
        """记录当日收盘后净值"""
        # 更新所有持仓的最新价格
        for code, pos in self.state["positions"].items():
            if code in price_map and price_map[code] > 0:
                pos["last_price"] = _round4(price_map[code])

        total_value = self.get_total_value(price_map)
        nav = total_value / self.initial_capital
        positions_value = total_value - self.state["cash"]

        record = {
            "date": current_date,
            "cash": _round2(self.state["cash"]),
            "positions_value": _round2(positions_value),
            "total_value": _round2(total_value),
            "nav": _round4(nav),
            "positions_count": len(self.state["positions"]),
        }
        self.state["daily_nav"].append(record)
        self.state["last_update"] = current_date

        if self.state.get("start_date") is None:
            self.state["start_date"] = current_date

        return record

    # ── 统计汇总 ──

    def get_summary(self):
        """生成当前状态汇总"""
        nav_history = self.state["daily_nav"]
        trades = self.state["trade_history"]

        current_value = self.state.get("_latest_total", self.get_total_value())
        nav = current_value / self.initial_capital

        # 最大回撤
        max_dd = 0
        peak = self.initial_capital
        for rec in nav_history:
            v = rec["total_value"]
            if v > peak:
                peak = v
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # 交易统计
        total_trades = len(trades)
        if total_trades > 0:
            wins = [t for t in trades if t["return_pct"] > 0]
            losses = [t for t in trades if t["return_pct"] <= 0]
            win_rate = len(wins) / total_trades * 100
            avg_return = np.mean([t["return_pct"] for t in trades])
            avg_win = np.mean([t["return_pct"] for t in wins]) if wins else 0
            avg_loss = abs(np.mean([t["return_pct"] for t in losses])) if losses else 0
            total_pnl = sum(t["pnl"] for t in trades)
            total_commission = sum(t.get("total_cost", 0) for t in trades)
        else:
            win_rate = avg_return = avg_win = avg_loss = 0
            total_pnl = total_commission = 0

        # 按轨道统计
        track_stats = {}
        for track in [0, 1]:
            t_trades = [t for t in trades if t["signal_track"] == track]
            if t_trades:
                t_wins = [t for t in t_trades if t["return_pct"] > 0]
                track_stats[track] = {
                    "count": len(t_trades),
                    "win_rate": _round2(len(t_wins) / len(t_trades) * 100),
                    "avg_return": _round2(np.mean([t["return_pct"] for t in t_trades])),
                    "total_pnl": _round2(sum(t["pnl"] for t in t_trades)),
                }

        # 退出原因分布
        exit_reasons = defaultdict(int)
        for t in trades:
            exit_reasons[t["exit_reason"]] += 1

        return {
            "initial_capital": self.initial_capital,
            "current_value": _round2(current_value),
            "cash": _round2(self.state["cash"]),
            "positions_count": len(self.state["positions"]),
            "nav": _round4(nav),
            "total_return_pct": _round2((nav - 1) * 100),
            "max_drawdown": _round2(max_dd),
            "total_trades": total_trades,
            "win_rate": _round2(win_rate),
            "avg_return": _round2(avg_return),
            "avg_win": _round2(avg_win),
            "avg_loss": _round2(avg_loss),
            "total_pnl": _round2(total_pnl),
            "total_commission": _round2(total_commission),
            "track_stats": track_stats,
            "exit_reasons": dict(exit_reasons),
            "trading_days": len(nav_history),
            "start_date": self.state.get("start_date"),
            "last_date": nav_history[-1]["date"] if nav_history else None,
        }

    def append_trade_log_csv(self):
        """将交易历史写入CSV日志"""
        trades = self.state["trade_history"]
        if not trades:
            return

        os.makedirs(os.path.dirname(TRADE_LOG_FILE), exist_ok=True)
        fieldnames = [
            "code", "name", "signal_date", "entry_date", "exit_date",
            "entry_price", "exit_price", "shares", "signal_track",
            "score_ml", "return_pct", "pnl", "exit_reason", "held_days",
            "commission_buy", "commission_sell", "stamp_duty", "total_cost",
        ]
        with open(TRADE_LOG_FILE, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in trades:
                writer.writerow({k: t.get(k, "") for k in fieldnames})
        print(f"[PaperTrader] 交易日志已保存: {TRADE_LOG_FILE}")

    def print_status(self, price_map=None):
        """打印当前状态"""
        total = self.get_total_value(price_map)
        nav = total / self.initial_capital
        pos_count = len(self.state["positions"])
        pending = len(self.state["pending_orders"])

        print(f"\n{'='*60}")
        print(f"66大顺 V2.2 纸上交易 — 当前状态")
        print(f"{'='*60}")
        print(f"  初始资金:   ¥{self.initial_capital:>14,.2f}")
        print(f"  当前现金:   ¥{self.state['cash']:>14,.2f}")
        print(f"  持仓数量:   {pos_count}只")
        print(f"  总资产:     ¥{total:>14,.2f}")
        print(f"  净值:       {nav:>14.4f}")
        print(f"  收益率:     {(nav-1)*100:>+13.2f}%")
        if pending:
            print(f"  挂单中:     {pending}笔（待次日执行）")

        if pos_count > 0:
            print(f"\n  当前持仓:")
            print(f"  {'代码':<8} {'名称':<8} {'轨道':<4} {'天数':<4} {'盈亏':>8} {'市值':>12}")
            print(f"  {'-'*52}")
            for code, pos in self.state["positions"].items():
                price = pos.get("last_price", pos["entry_price"])
                if price_map and code in price_map:
                    price = price_map[code]
                ret = (price / pos["entry_price"] - 1) * 100
                mv = pos["shares"] * price
                track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
                print(f"  {code:<8} {pos['name']:<8} {track_name:<4} "
                      f"{pos.get('held_days',0):<4} {ret:>+7.2f}% ¥{mv:>11,.0f}")
        print(f"{'='*60}")

    def build_daily_report(self, current_date: str) -> str:
        """构建每日交易报告（控制台 + 邮件）

        current_date: 当日日期 YYYYMMDD
        返回: 格式化文本报告
        """
        fmt_date = f"{current_date[:4]}-{current_date[4:6]}-{current_date[6:8]}"
        lines = []
        sep = "=" * 60
        thin = "-" * 60

        total_value = self.get_total_value()
        nav = total_value / self.initial_capital
        ret_pct = (nav - 1) * 100

        # ── 标题 ──
        lines.append(sep)
        lines.append(f"  66大顺 V2.2 纸上交易日报 ({fmt_date})")
        lines.append(sep)
        lines.append("")

        # ── 账户概览 ──
        lines.append("【账户概览】")
        lines.append(f"  初始资金:   ¥{self.initial_capital:>12,.2f}")
        lines.append(f"  当前现金:   ¥{self.state['cash']:>12,.2f}")
        lines.append(f"  持仓市值:   ¥{total_value - self.state['cash']:>12,.2f}")
        lines.append(f"  总资产:     ¥{total_value:>12,.2f}")
        lines.append(f"  累计收益:   {ret_pct:>+11.2f}%")
        lines.append(f"  净值:       {nav:>12.4f}")
        lines.append("")

        # ── 当日买入 ──
        today_buys = []
        for code, pos in self.state["positions"].items():
            if pos.get("entry_date") == current_date:
                today_buys.append((code, pos))

        lines.append("【当日买入】")
        if today_buys:
            lines.append(f"  {'代码':<8} {'名称':<8} {'轨道':<4} "
                         f"{'价格':>8} {'数量':>6} {'金额':>12}")
            lines.append(f"  {thin[:52]}")
            for code, pos in today_buys:
                track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
                amount = pos["shares"] * pos["entry_price"]
                lines.append(
                    f"  {code:<8} {pos['name']:<8} {track_name:<4} "
                    f"¥{pos['entry_price']:>7.2f} {pos['shares']:>5} "
                    f"¥{amount:>10,.0f}"
                )
            lines.append(f"  买入合计: {len(today_buys)}笔, "
                         f"¥{sum(p['shares'] * p['entry_price'] for _, p in today_buys):,.0f}")
        else:
            lines.append("  今日无买入")
        lines.append("")

        # ── 当日卖出 ──
        today_sells = [t for t in self.state.get("trade_history", [])
                       if t.get("exit_date") == current_date]

        lines.append("【当日卖出】")
        if today_sells:
            lines.append(f"  {'代码':<8} {'名称':<8} {'轨道':<4} "
                         f"{'价格':>8} {'数量':>6} {'盈亏':>10} {'原因':<16}")
            lines.append(f"  {thin[:52]}")
            for t in today_sells:
                track_name = "A" if t.get("signal_track", 0) == 0 else "B"
                lines.append(
                    f"  {t['code']:<8} {t['name']:<8} {track_name:<4} "
                    f"¥{t['exit_price']:>7.2f} {t['shares']:>5} "
                    f"{t['return_pct']:>+8.2f}% {t['exit_reason']:<16}"
                )
            total_pnl = sum(t["pnl"] for t in today_sells)
            lines.append(f"  卖出合计: {len(today_sells)}笔, "
                         f"盈亏 ¥{total_pnl:+,.0f}")
        else:
            lines.append("  今日无卖出")
        lines.append("")

        # ── 当前持仓 ──
        positions = self.state.get("positions", {})
        lines.append("【当前持仓】")
        if positions:
            lines.append(f"  {'代码':<8} {'名称':<8} {'轨道':<4} "
                         f"{'天数':>4} {'数量':>6} {'买入价':>8} "
                         f"{'现价':>8} {'盈亏':>8} {'市值':>12}")
            lines.append(f"  {thin[:52]}{'-'*28}")

            total_pos_value = 0
            for code, pos in positions.items():
                price = pos.get("last_price", pos["entry_price"])
                ret = (price / pos["entry_price"] - 1) * 100
                mv = pos["shares"] * price
                total_pos_value += mv
                track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
                lines.append(
                    f"  {code:<8} {pos['name']:<8} {track_name:<4} "
                    f"{pos.get('held_days', 0):>4} {pos['shares']:>5} "
                    f"¥{pos['entry_price']:>7.2f} "
                    f"¥{price:>7.2f} {ret:>+7.2f}% ¥{mv:>10,.0f}"
                )
            lines.append(f"  持仓总市值: ¥{total_pos_value:,.0f}  "
                         f"({len(positions)}只)")
        else:
            lines.append("  当前无持仓")
        lines.append("")

        # ── 待执行挂单 ──
        pending = self.state.get("pending_orders", [])
        if pending:
            lines.append("【待执行挂单（次日开盘买入）】")
            for o in pending:
                track_name = "A" if o.get("signal_track", 0) == 0 else "B"
                lines.append(f"  [{track_name}] {o['code']} {o['name']}  "
                             f"ML={o.get('score_ml', 0)}")
            lines.append("")

        lines.append(sep)
        lines.append(f"  报告时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(sep)

        return "\n".join(lines)
