"""66大顺 V1.6 执行层 — 三阶信号退出规则与组合模拟

Tier A: V1.0 Core — Pure Hold (7/5/3天)
Tier B: 延续金叉扩容 — 移动止盈(10%/4%) + 硬止损(-8%) + 时间止损(5天)
Tier C: ML门控弹性扩容 — 紧移动止盈(8%/3%) + 硬止损(-6%) + 时间止损(4天)

Version: 1.6
Date: 2026-06-06
"""

from collections import defaultdict
import numpy as np
import pandas as pd


# ── V1.6 配置 ──────────────────────────────────────────
V1_6_CONFIG = {
    # Tier A: V1.0 核心（纯持有到期）
    'tier_a': {
        'ml_min': 10,
        'enhanced_rules_min': 1,
        'hold_days': {13: 7, 11: 5, 10: 3},
        'max_position_pct': 0.05,
    },
    # Tier B: 延续金叉扩容（主动退出）
    'tier_b': {
        'ml_min': 11,
        'enhanced_rules_min': 1,
        'hold_days': {13: 5, 11: 3, 10: 3},
        'max_position_pct': 0.04,
        'hard_stop_pct': -0.08,           # -8%硬止损
        'trailing_activate_pct': 0.10,    # +10%激活移动止盈
        'trailing_stop_pct': -0.04,       # 从峰回落4%触发
        'max_hold_days': 5,               # 最长持有天数
    },
    # Tier C: ML门控弹性扩容（紧退出）
    'tier_c': {
        'ml_min': 13,
        'enhanced_rules_min': 2,
        'hold_days': {13: 4, 11: 3},
        'max_position_pct': 0.02,
        'hard_stop_pct': -0.06,           # -6%硬止损
        'trailing_activate_pct': 0.08,    # +8%激活移动止盈
        'trailing_stop_pct': -0.03,       # 从峰回落3%触发
        'max_hold_days': 4,               # 最长持有天数
    },
    # 市场状态仓位乘数 (tier_id -> {market_state: mult})
    'market_mult': {
        0: {'strong': 1.0, 'choppy': 0.5, 'weak_reduced': 0.0, 'weak': 0.0},
        1: {'strong': 1.0, 'choppy': 0.0, 'weak_reduced': 0.0, 'weak': 0.0},
        2: {'strong': 1.0, 'choppy': 0.0, 'weak_reduced': 0.0, 'weak': 0.0},
    },
    'weak_market_ml_threshold': 14,
}


def _get_price_at_date(stock_df: pd.DataFrame, date_str: str) -> float:
    """从stock_df中获取指定日期的收盘价"""
    sub = stock_df[stock_df["date"].astype(str) == date_str]
    if sub.empty:
        return 0.0
    return float(sub.iloc[-1]["close"])


def check_tier_b_exit(pos: dict, current_price: float) -> str:
    """Tier B 退出检查：硬止损 + 移动止盈 + 时间止损"""
    cfg = V1_6_CONFIG['tier_b']
    entry_price = pos["entry_price"]
    ret = (current_price / entry_price - 1) if entry_price > 0 else 0

    # 更新最高价
    if current_price > pos.get("peak_price", entry_price):
        pos["peak_price"] = current_price

    # 1) 硬止损
    if ret <= cfg['hard_stop_pct']:
        return "B_HARD_STOP"

    # 2) 移动止盈
    peak = pos.get("peak_price", entry_price)
    if peak > entry_price * (1 + cfg['trailing_activate_pct']):
        dd = (peak - current_price) / peak
        if dd >= abs(cfg['trailing_stop_pct']):
            return "B_TRAILING_STOP"

    # 3) 时间止损
    held = pos.get("held_days", 0)
    if held >= cfg['max_hold_days']:
        return "B_TIME_STOP"

    return None


def check_tier_c_exit(pos: dict, current_price: float) -> str:
    """Tier C 退出检查：紧硬止损 + 紧移动止盈 + 时间止损"""
    cfg = V1_6_CONFIG['tier_c']
    entry_price = pos["entry_price"]
    ret = (current_price / entry_price - 1) if entry_price > 0 else 0

    # 更新最高价
    if current_price > pos.get("peak_price", entry_price):
        pos["peak_price"] = current_price

    # 1) 硬止损
    if ret <= cfg['hard_stop_pct']:
        return "C_HARD_STOP"

    # 2) 移动止盈
    peak = pos.get("peak_price", entry_price)
    if peak > entry_price * (1 + cfg['trailing_activate_pct']):
        dd = (peak - current_price) / peak
        if dd >= abs(cfg['trailing_stop_pct']):
            return "C_TRAILING_STOP"

    # 3) 时间止损
    held = pos.get("held_days", 0)
    if held >= cfg['max_hold_days']:
        return "C_TIME_STOP"

    return None


def get_tier_hold_days(ml_score: float, tier: int) -> int:
    """根据ML评分和tier获取持有天数"""
    if tier == 0:
        hold_map = V1_6_CONFIG['tier_a']['hold_days']
    elif tier == 1:
        hold_map = V1_6_CONFIG['tier_b']['hold_days']
    elif tier == 2:
        hold_map = V1_6_CONFIG['tier_c']['hold_days']
    else:
        hold_map = {13: 7, 11: 5, 10: 3}

    result = 3
    for threshold, days in sorted(hold_map.items(), reverse=True):
        if ml_score >= threshold:
            result = days
            break
    return result


def get_tier_position_pct(tier: int) -> float:
    """获取tier对应的单仓最大比例"""
    if tier == 0:
        return V1_6_CONFIG['tier_a']['max_position_pct']
    elif tier == 1:
        return V1_6_CONFIG['tier_b']['max_position_pct']
    elif tier == 2:
        return V1_6_CONFIG['tier_c']['max_position_pct']
    return 0.05


def get_market_multiplier(tier: int, market_state: str) -> float:
    """获取tier在某市场状态下的仓位乘数"""
    mult_map = V1_6_CONFIG['market_mult'].get(tier, {})
    return mult_map.get(market_state, 0.0)


def simulate_v16_portfolio(
    signals_df: pd.DataFrame,
    stock_daily: dict,
    trading_dates: list,
    initial_capital: float = 1_000_000,
) -> dict:
    """V1.6 组合模拟 — 三阶差异化退出

    signals_df 必须包含 'signal_tier' 列 (0=Tier A, 1=Tier B, 2=Tier C)
    """
    if signals_df.empty:
        return {"total_trades": 0, "final_value": initial_capital, "total_return": 0}

    signals_df = signals_df.copy()
    signals_df["date"] = signals_df["date"].astype(str)
    signals = signals_df.to_dict("records")
    signal_lookup = defaultdict(list)
    for sig in signals:
        signal_lookup[sig["date"]].append(sig)

    all_dates = sorted(trading_dates) if trading_dates else sorted(signal_lookup.keys())
    if not all_dates:
        return {"total_trades": 0}

    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    active = {}       # {code: pos_dict}
    closed_trades = []
    daily_nav = []
    capital = initial_capital

    for date_str in all_dates:
        today_signals_raw = signal_lookup.get(date_str, [])
        today_idx = date_to_idx.get(date_str)

        # ── 检查持仓退出 ──
        to_close = []
        for code, pos in list(active.items()):
            stock_df = stock_daily.get(code)
            if stock_df is None or stock_df.empty:
                exit_price = pos["entry_price"]
                to_close.append((code, pos, exit_price, date_str, "NO_DATA"))
                continue
            current_price = _get_price_at_date(stock_df, date_str)
            if current_price <= 0:
                continue

            pos["held_days"] = pos.get("held_days", 0) + 1
            tier = pos.get("signal_tier", 0)
            exit_reason = None

            if tier == 0:
                # Tier A: Pure Hold — 到期卖出
                exit_idx = pos.get("exit_idx")
                if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                    exit_reason = "A_HOLD_EXPIRY"
            elif tier == 1:
                # Tier B: 动态退出
                exit_reason = check_tier_b_exit(pos, current_price)
                if exit_reason is None:
                    # 也检查到期
                    exit_idx = pos.get("exit_idx")
                    if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                        exit_reason = "B_HOLD_EXPIRY"
            elif tier == 2:
                # Tier C: 紧动态退出
                exit_reason = check_tier_c_exit(pos, current_price)
                if exit_reason is None:
                    exit_idx = pos.get("exit_idx")
                    if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                        exit_reason = "C_HOLD_EXPIRY"

            if exit_reason:
                to_close.append((code, pos, current_price, date_str, exit_reason))

        # 执行卖出
        for code, pos, exit_price, exit_date, reason in to_close:
            ret = (exit_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0
            trade_pnl = pos["position_size"] * ret / 100
            capital += pos["position_size"] + trade_pnl
            closed_trades.append({
                "code": code,
                "entry_date": pos["entry_date"],
                "exit_date": exit_date,
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "position_size": pos["position_size"],
                "return_pct": round(ret, 2),
                "pnl": round(trade_pnl, 2),
                "exit_reason": reason,
                "entry_score_ml": pos.get("entry_score_ml", 0),
                "signal_tier": pos.get("signal_tier", 0),
                "held_days": pos.get("held_days", 0),
            })
            del active[code]

        # ── 当日新信号买入 ──
        if today_signals_raw:
            # 按市场状态分配每日信号上限
            ms = today_signals_raw[0].get("market_state", "strong") if today_signals_raw else "strong"

            # 按tier分组排序
            tier0 = sorted([s for s in today_signals_raw if s.get("signal_tier") == 0],
                           key=lambda x: x.get("score_ml", 0), reverse=True)
            tier1 = sorted([s for s in today_signals_raw if s.get("signal_tier") == 1],
                           key=lambda x: x.get("score_ml", 0), reverse=True)
            tier2 = sorted([s for s in today_signals_raw if s.get("signal_tier") == 2],
                           key=lambda x: x.get("score_ml", 0), reverse=True)

            # 每日各tier上限
            if ms == "strong":
                max_t0, max_t1, max_t2 = 3, 2, 1
            elif ms == "choppy":
                max_t0, max_t1, max_t2 = 2, 0, 0
            else:
                max_t0, max_t1, max_t2 = 0, 0, 0

            tier0_selected = tier0[:max_t0]
            tier1_selected = tier1[:max_t1]
            tier2_selected = tier2[:max_t2]

            for sig in tier0_selected + tier1_selected + tier2_selected:
                code = sig["code"]
                if code in active:
                    continue

                tier = sig.get("signal_tier", 0)
                mult = get_market_multiplier(tier, ms)
                if mult <= 0:
                    continue

                stock_df = stock_daily.get(code)
                if stock_df is None:
                    continue
                entry_price = _get_price_at_date(stock_df, date_str)
                if entry_price <= 0:
                    continue

                pos_pct = get_tier_position_pct(tier)
                pos_size = capital * pos_pct * mult
                pos_size = min(pos_size, capital * 0.95)
                if pos_size <= 0:
                    continue

                capital -= pos_size
                ml = sig.get("score_ml", 10)
                hold_days = get_tier_hold_days(ml, tier)
                exit_idx = today_idx + hold_days if today_idx is not None else None

                active[code] = {
                    "entry_date": date_str,
                    "entry_price": entry_price,
                    "position_size": pos_size,
                    "entry_score_ml": ml,
                    "signal_tier": tier,
                    "exit_idx": exit_idx,
                    "peak_price": entry_price,
                    "held_days": 0,
                }

        # 记录每日净值
        nav = capital + sum(p["position_size"] for p in active.values())
        daily_nav.append({"date": date_str, "nav": nav})

    # ── 计算最终指标 ──
    final_value = daily_nav[-1]["nav"] if daily_nav else capital
    total_return = (final_value / initial_capital - 1) * 100

    # 年化
    if len(all_dates) > 0:
        years = len(all_dates) / 252
        ann_return = ((final_value / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0
    else:
        ann_return = 0

    # 最大回撤
    nav_values = [d["nav"] for d in daily_nav]
    max_dd = 0
    peak = nav_values[0] if nav_values else initial_capital
    for v in nav_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # 夏普比率
    daily_returns = []
    for i in range(1, len(nav_values)):
        if nav_values[i - 1] > 0:
            daily_returns.append(nav_values[i] / nav_values[i - 1] - 1)
    if len(daily_returns) > 5:
        avg_ret = np.mean(daily_returns)
        std_ret = np.std(daily_returns, ddof=1)
        sharpe = (avg_ret - 0.02 / 252) / std_ret * np.sqrt(252) if std_ret > 0 else 0
    else:
        sharpe = 0

    # 胜率统计
    if closed_trades:
        wins = [t for t in closed_trades if t["return_pct"] > 0]
        losses = [t for t in closed_trades if t["return_pct"] <= 0]
        win_rate = len(wins) / len(closed_trades) * 100
        avg_win = np.mean([t["return_pct"] for t in wins]) if wins else 0
        avg_loss = abs(np.mean([t["return_pct"] for t in losses])) if losses else 0
        profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        profit_factor = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if sum(t["pnl"] for t in losses) != 0 else float("inf")
    else:
        win_rate = profit_ratio = profit_factor = 0

    # 退出原因统计
    exit_reasons = {}
    for trade in closed_trades:
        r = trade["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # 按tier统计
    tier_stats = {}
    for tier in [0, 1, 2]:
        tier_trades = [t for t in closed_trades if t["signal_tier"] == tier]
        if tier_trades:
            tier_wins = [t for t in tier_trades if t["return_pct"] > 0]
            tier_stats[tier] = {
                "count": len(tier_trades),
                "win_rate": len(tier_wins) / len(tier_trades) * 100,
                "avg_return": np.mean([t["return_pct"] for t in tier_trades]),
            }

    return {
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "ann_return": round(ann_return, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "win_rate": round(win_rate, 1),
        "profit_ratio": round(profit_ratio, 2),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(closed_trades),
        "exit_reasons": exit_reasons,
        "tier_stats": tier_stats,
        "daily_nav": daily_nav,
        "closed_trades": closed_trades,
        "all_dates": all_dates,
    }
