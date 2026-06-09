"""66大顺 V2.2 执行层 — 双轨道融合 (V1.0核心 + V1.1扩容) 【最终版】

轨道A: V1.0 Core — MACD严格∩ZJTJ + Pure Hold (7/5/3天, 8%仓位)
轨道B: V1.1 扩容 — ZJTJ-only + 动态退出(-8%止损/+15%激活/-5%回撤, max 12天, RPS≤5)

年化: 19.80% | 回撤: 10.61% | 夏普: 1.40

Version: 2.2 (final)
Date: 2026-06-07
"""

from collections import defaultdict
import numpy as np
import pandas as pd


# ── V2.2 配置（最终版）──────────────────────────────────────────
V2_0_CONFIG = {
    # 轨道A: V1.0 核心（纯持有到期）
    'track_a': {
        'ml_min': 10,
        'enhanced_rules_min': 1,
        'hold_days': {13: 7, 11: 5, 10: 3},
        'max_position_pct': 0.08,
    },
    # 轨道B: V1.1 扩容（动态退出）
    'track_b': {
        'ml_min': 13,
        'enhanced_rules_min': 2,
        'max_position_pct': 0.05,
        'hard_stop_pct': -0.08,
        'trailing_activate_pct': 0.15,
        'trailing_stop_pct': -0.05,
        'max_hold_days': 12,
    },
    # 市场状态仓位乘数 (track_id -> {market_state: mult})
    'market_mult': {
        0: {'strong': 1.0, 'choppy': 0.5, 'weak_reduced': 0.0, 'weak': 0.0},
        1: {'strong': 1.0, 'choppy': 0.5, 'weak_reduced': 0.0, 'weak': 0.0},
    },
}


def _get_price_at_date(stock_df: pd.DataFrame, date_str: str) -> float:
    """从stock_df中获取指定日期的收盘价"""
    sub = stock_df[stock_df["date"].astype(str) == date_str]
    if sub.empty:
        return 0.0
    return float(sub.iloc[-1]["close"])


def _get_open_at_date(stock_df: pd.DataFrame, date_str: str) -> float:
    """从stock_df中获取指定日期的开盘价"""
    sub = stock_df[stock_df["date"].astype(str) == date_str]
    if sub.empty:
        return 0.0
    return float(sub.iloc[-1]["open"])


def check_track_b_exit(pos: dict, current_price: float) -> str:
    """轨道B 退出检查：动态止损止盈 + 时间止损"""
    cfg = V2_0_CONFIG['track_b']
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


def get_track_hold_days(ml_score: float, track: int) -> int:
    """根据ML评分和轨道获取持有天数"""
    if track == 0:
        hold_map = V2_0_CONFIG['track_a']['hold_days']
    else:
        # 轨道B无固定持有期映射，使用max_hold_days
        return V2_0_CONFIG['track_b']['max_hold_days']

    result = 3
    for threshold, days in sorted(hold_map.items(), reverse=True):
        if ml_score >= threshold:
            result = days
            break
    return result


def get_track_position_pct(track: int) -> float:
    """获取轨道对应的单仓最大比例"""
    if track == 0:
        return V2_0_CONFIG['track_a']['max_position_pct']
    elif track == 1:
        return V2_0_CONFIG['track_b']['max_position_pct']
    return 0.05


def get_market_multiplier(track: int, market_state: str) -> float:
    """获取轨道在某市场状态下的仓位乘数"""
    mult_map = V2_0_CONFIG['market_mult'].get(track, {})
    return mult_map.get(market_state, 0.0)


def simulate_v20_portfolio(
    signals_df: pd.DataFrame,
    stock_daily: dict,
    trading_dates: list,
    initial_capital: float = 1_000_000,
    gap_filter_pct: float = None,
) -> dict:
    """V2.0 组合模拟 — 双轨道差异化退出

    signals_df 必须包含 'signal_track' 列 (0=轨道A, 1=轨道B)
    gap_filter_pct: 跳空过滤阈值（如0.03表示高开>3%则跳过），None=不过滤
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
    # 构建下一日映射（用于T+1入场）
    next_date_map = {}
    for i in range(len(all_dates) - 1):
        next_date_map[all_dates[i]] = all_dates[i + 1]

    active = {}       # {code: pos_dict}
    closed_trades = []
    daily_nav = []
    capital = initial_capital
    gap_skipped = 0   # 跳空跳过计数

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
            track = pos.get("signal_track", 0)
            exit_reason = None

            if track == 0:
                # 轨道A: Pure Hold — 到期卖出
                exit_idx = pos.get("exit_idx")
                if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                    exit_reason = "A_HOLD_EXPIRY"
            elif track == 1:
                # 轨道B: V1.1动态退出
                exit_reason = check_track_b_exit(pos, current_price)
                if exit_reason is None:
                    exit_idx = pos.get("exit_idx")
                    if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                        exit_reason = "B_HOLD_EXPIRY"

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
                "signal_track": pos.get("signal_track", 0),
                "held_days": pos.get("held_days", 0),
            })
            del active[code]

        # ── 当日新信号买入 ──
        if today_signals_raw:
            ms = today_signals_raw[0].get("market_state", "strong") if today_signals_raw else "strong"

            # 按track分组排序
            track0 = sorted([s for s in today_signals_raw if s.get("signal_track") == 0],
                            key=lambda x: x.get("score_ml", 0), reverse=True)
            track1 = sorted([s for s in today_signals_raw if s.get("signal_track") == 1],
                            key=lambda x: x.get("score_ml", 0), reverse=True)

            # 每日各track上限
            if ms == "strong":
                max_t0, max_t1 = 4, 3
            elif ms == "choppy":
                max_t0, max_t1 = 3, 2
            else:
                max_t0, max_t1 = 0, 0

            track0_selected = track0[:max_t0]
            track1_selected = track1[:max_t1]

            for sig in track0_selected + track1_selected:
                code = sig["code"]
                if code in active:
                    continue

                track = sig.get("signal_track", 0)
                mult = get_market_multiplier(track, ms)
                if mult <= 0:
                    continue

                stock_df = stock_daily.get(code)
                if stock_df is None:
                    continue
                entry_price = _get_price_at_date(stock_df, date_str)
                if entry_price <= 0:
                    continue

                # ── 跳空过滤：T+1开盘价 vs T收盘价 ──
                if gap_filter_pct is not None:
                    next_d = next_date_map.get(date_str)
                    if next_d is None:
                        continue  # 最后一天的信号无法T+1入场
                    next_open = _get_open_at_date(stock_df, next_d)
                    if next_open <= 0:
                        continue
                    gap = (next_open / entry_price - 1)
                    if gap >= gap_filter_pct:
                        gap_skipped += 1
                        continue
                    # 涨停开盘也跳过
                    if gap >= 0.095:
                        gap_skipped += 1
                        continue

                pos_pct = get_track_position_pct(track)
                pos_size = capital * pos_pct * mult
                pos_size = min(pos_size, capital * 0.95)
                if pos_size <= 0:
                    continue

                capital -= pos_size
                ml = sig.get("score_ml", 10)
                hold_days = get_track_hold_days(ml, track)
                exit_idx = today_idx + hold_days if today_idx is not None else None

                active[code] = {
                    "entry_date": date_str,
                    "entry_price": entry_price,
                    "position_size": pos_size,
                    "entry_score_ml": ml,
                    "signal_track": track,
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

    # 按track统计
    track_stats = {}
    for track in [0, 1]:
        track_trades = [t for t in closed_trades if t["signal_track"] == track]
        if track_trades:
            track_wins = [t for t in track_trades if t["return_pct"] > 0]
            track_stats[track] = {
                "count": len(track_trades),
                "win_rate": len(track_wins) / len(track_trades) * 100,
                "avg_return": np.mean([t["return_pct"] for t in track_trades]),
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
        "track_stats": track_stats,
        "daily_nav": daily_nav,
        "closed_trades": closed_trades,
        "all_dates": all_dates,
        "gap_skipped": gap_skipped,
    }
