"""组合管理器 - 仓位计算、动态止损止盈、回测资金曲线模拟

Level 1 改进核心模块。
提供三个功能：
1. PositionSizer - 基于评分的仓位分配
2. DynamicExitRules - 多条件退出规则引擎
3. simulate_portfolio - 完整回测资金曲线模拟
"""
import warnings
from collections import defaultdict
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

from config.settings import (
    POSITION_SIZING_ENABLED, POSITION_BASE_PCT, POSITION_MAX_PCT,
    POSITION_SCORE_WEIGHT, POSITION_KELLY_FRACTION,
    TRAILING_STOP_LOSS, HARD_STOP_LOSS,
    TIME_STOP_LOSS_DAYS, TIME_STOP_LOSS_MIN_RETURN,
    TAKE_PROFIT_TARGET, SCORE_STOP_THRESHOLD, EXIT_CHECK_FREQ,
    ATR_STOP_ENABLED, ATR_PERIOD, ATR_STOP_MULTIPLIER, ATR_TRAILING_MULTIPLIER,
    KELLY_ML_13, KELLY_ML_11, KELLY_ML_10,
    DYNAMIC_HOLD_ML_HIGH, DYNAMIC_HOLD_ML_MID, ML_SCORE_MIN_THRESHOLD,
    PURE_STOP_LOSS_PCT, TAKE_PROFIT_TRIGGER, TAKE_PROFIT_SELL_RATIO,
    MAX_DAILY_STRONG, MAX_DAILY_CHOPPY, MAX_DAILY_WEAK,
    TRAILING_STOP_ENABLED, TRAILING_STOP_FROM_PEAK_PCT, TRAILING_STOP_ACTIVATE_AT,
)

# ─────────────────────────────────────────────
# A. PositionSizer - 仓位计算器
# ─────────────────────────────────────────────


def _ml_weight(score_ml: float) -> float:
    """ML评分权重映射（Phase 4 Kelly精细版）: 高分=重仓, 低分=轻仓
    
    结合Kelly分数控制每个评分的仓位比例:
    - ML >= 13: 全仓 Kelly (100%)
    - ML >= 11: 75% Kelly
    - ML >= 10: 50% Kelly
    - 其余: 不买入（已被ML阈值过滤）
    """
    if score_ml >= 13:
        return KELLY_ML_13
    elif score_ml >= 11:
        return KELLY_ML_11
    elif score_ml >= ML_SCORE_MIN_THRESHOLD:
        return KELLY_ML_10
    return 0.0


def calc_position_size(
    total_score: int,
    score_ml: float,
    max_score: int,
    total_weighted: float,
    total_capital: float,
) -> float:
    """计算单个信号的仓位金额

    参数:
        total_score: 该信号综合评分
        score_ml: ML 评分 (0-15)
        max_score: 最大可能综合评分
        total_weighted: 当日所有信号加权分之和
        total_capital: 当日总可用资金

    返回:
        分配仓位金额
    """
    if not POSITION_SIZING_ENABLED:
        return total_capital  # 未启用=全仓

    if total_weighted <= 0 or max_score <= 0:
        return total_capital * POSITION_BASE_PCT

    # 按ML评分分层加权
    if POSITION_SCORE_WEIGHT:
        weight = _ml_weight(score_ml) * (total_score / max_score)
    else:
        weight = total_score / max_score

    # 归一化到当日总加权
    norm_weight = weight / total_weighted

    # 计算仓位
    position = norm_weight * (total_capital * 0.80)  # 总仓位上限80%

    # 单仓上下限
    position = max(position, total_capital * POSITION_BASE_PCT)
    position = min(position, total_capital * POSITION_MAX_PCT)

    return position


def calc_all_positions(
    today_signals: list,
    available_capital: float,
) -> list:
    """批量计算当日所有信号仓位

    参数:
        today_signals: [{total_score, score_ml, max_score, ...}, ...]
        available_capital: 可用资金

    返回:
        [{**signal, position_size: float}, ...]
    """
    if not today_signals:
        return []

    max_score = today_signals[0].get("max_score", 100)
    total_weighted = 0.0
    for sig in today_signals:
        if POSITION_SCORE_WEIGHT:
            w = _ml_weight(sig.get("score_ml", 0)) * (sig.get("total_score", 0) / max_score)
        else:
            w = sig.get("total_score", 0) / max_score
        total_weighted += w

    results = []
    for sig in today_signals:
        size = calc_position_size(
            sig.get("total_score", 0),
            sig.get("score_ml", 0),
            max_score,
            total_weighted,
            available_capital,
        )
        results.append({**sig, "position_size": size})

    return results


# ─────────────────────────────────────────────
# B. DynamicExitRules - 退出规则引擎
# ─────────────────────────────────────────────


class Position:
    """单个持仓记录"""

    def __init__(self, signal: dict, entry_price: float, position_size: float, entry_idx: int):
        self.code = signal["code"]
        self.entry_date = signal["date"]
        self.entry_price = entry_price
        self.position_size = position_size  # 投入金额
        self.held_days = 0
        self.highest_price = entry_price
        self.current_price = entry_price
        self.entry_idx = entry_idx
        self.entry_score_ml = signal.get("score_ml", 0)
        self.entry_total_score = signal.get("total_score", 0)
        self.exit_reason = None
        self.exit_price = None
        self.exit_date = None
        self.reduced = False  # 是否已经触发目标止盈减仓
        self.shares = 0.0    # 持仓股数（实际买入时确定）
        self._cache_current_ml = None
        self.atr_value = 0.0       # 入场时ATR值（用于ATR止损）
        self.entry_high = entry_price
        self.entry_low = entry_price


def _calc_atr(stock_df: pd.DataFrame, period: int = 14) -> float:
    """计算股票最新ATR（平均真实波幅）
    
    参数:
        stock_df: 日线DataFrame [date, open, high, low, close]
        period: ATR计算周期，默认14
    
    返回:
        ATR值（股价单位），如数据不足返回股价的5%作为默认值
    """
    if stock_df is None or len(stock_df) < period + 1:
        return stock_df["close"].iloc[-1] * 0.05 if stock_df is not None and not stock_df.empty else 0.0
    
    high = stock_df["high"].values
    low = stock_df["low"].values
    close = stock_df["close"].values
    
    tr_list = []
    for i in range(1, len(stock_df)):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i-1])
        lc = abs(low[i] - close[i-1])
        tr_list.append(max(hl, hc, lc))
    
    if len(tr_list) < period:
        return stock_df["close"].iloc[-1] * 0.05
    
    # SMA of TR
    tr_array = np.array(tr_list[-period:])
    atr = float(np.mean(tr_array))
    return atr if atr > 0 else stock_df["close"].iloc[-1] * 0.05


def check_exit_rules(
    position: Position,
    current_bar: pd.Series,
    current_date: str,
    current_ml_score: Optional[float],
    stock_df: pd.DataFrame = None,
) -> Optional[str]:
    """检查所有退出条件（Phase 4: 支持ATR波动率止损）

    参数:
        position: 持仓对象
        current_bar: 当日的股票K线
        current_date: 当前日期
        current_ml_score: 当日ML评分（None=无法计算）
        stock_df: 完整的股票日线DataFrame（用于ATR计算，可选）

    返回:
        退出原因字符串，None=继续持有
    """
    current_price = current_bar["close"]
    position.current_price = current_price
    position.held_days += 1

    # 更新区间最高价
    if current_price > position.highest_price:
        position.highest_price = current_price

    # 累计盈亏
    ret = (current_price / position.entry_price - 1) if position.entry_price > 0 else 0

    # ── 规则1: ATR波动率硬止损 ──
    if ATR_STOP_ENABLED and position.atr_value > 0:
        stop_price = position.entry_price - ATR_STOP_MULTIPLIER * position.atr_value
        if current_price <= stop_price:
            position.exit_price = current_price
            position.exit_date = current_date
            return "ATR_HARD_STOP"
    else:
        # 回退到固定比例硬止损
        if ret <= -HARD_STOP_LOSS:
            position.exit_price = current_price
            position.exit_date = current_date
            return "HARD_STOP"

    # ── 规则2: ATR跟踪止损（从最高点回撤） ──
    if ATR_STOP_ENABLED and position.atr_value > 0:
        trailing_dist = ATR_TRAILING_MULTIPLIER * position.atr_value / position.entry_price
        if position.highest_price > position.entry_price * 1.02:
            drawdown = (position.highest_price - current_price) / position.highest_price
            if drawdown >= trailing_dist:
                position.exit_price = current_price
                position.exit_date = current_date
                return "ATR_TRAILING_STOP"
    else:
        # 回退到固定比例跟踪止损
        if position.highest_price > position.entry_price * 1.02:
            drawdown = (position.highest_price - current_price) / position.highest_price
            if drawdown >= TRAILING_STOP_LOSS:
                position.exit_price = current_price
                position.exit_date = current_date
                return "TRAILING_STOP"

    # ── 规则3: 时间止损 ──
    if position.held_days >= TIME_STOP_LOSS_DAYS and ret < TIME_STOP_LOSS_MIN_RETURN:
        position.exit_price = current_price
        position.exit_date = current_date
        return "TIME_STOP"

    # ── 规则4: 目标止盈（减半仓） ──
    if not position.reduced and ret >= TAKE_PROFIT_TARGET:
        position.reduced = True
        # 减仓50%：卖出半仓，剩余的继续持有
        # 调整持仓：视为已经卖出一半，剩余仓位继续
        position.position_size *= 0.5
        position.entry_price = current_price  # 重置成本为当前价，方便跟踪剩余仓位
        position.highest_price = current_price
        return None  # 不减半仓了，简化为不止盈，继续持有

    # ── 规则5: ML评分失效（用入场时评分为准，避免未来偏差） ──
    if position.entry_score_ml < SCORE_STOP_THRESHOLD and position.held_days >= 5:
        # 入场时ML评分就低于阈值且已持有5天仍未改善 → 退出
        position.exit_price = current_price
        position.exit_date = current_date
        return "SCORE_STOP"

    return None


# ─────────────────────────────────────────────
# C. PortfolioSimulator - 回测资金曲线模拟
# ─────────────────────────────────────────────


def _build_signal_lookup(signals: list) -> dict:
    """将信号列表按日期分组: {date_str: [{signal}, ...]}"""
    lookup = defaultdict(list)
    for sig in signals:
        lookup[sig["date"]].append(sig)
    return dict(lookup)


def _get_price_at_date(stock_df: pd.DataFrame, date_str: str, col: str = "close") -> float:
    """获取某只股票在指定日期的价格"""
    dates = pd.to_datetime(stock_df["date"])
    for i, d in enumerate(dates):
        if d >= pd.Timestamp(date_str):
            return float(stock_df[col].iloc[i])
    return 0.0


def _get_row_at_date(stock_df: pd.DataFrame, date_str: str) -> Optional[pd.Series]:
    """获取某只股票在指定日期的K线"""
    dates = pd.to_datetime(stock_df["date"])
    for i, d in enumerate(dates):
        if d == pd.Timestamp(date_str):
            return stock_df.iloc[i]
        if d > pd.Timestamp(date_str):
            return stock_df.iloc[i]  # 取最近的一根
    return None


def _daily_ml_score(stock_df: pd.DataFrame, rps_rank: int, rps_top_n: int) -> float:
    """计算股票当前ML评分（用于score_stop检查）"""
    try:
        return ml_scorer.predict_score(stock_df, rps_rank=rps_rank, rps_top_n=rps_top_n)
    except Exception:
        return None


def simulate_portfolio(
    signals_df: pd.DataFrame,
    stock_daily: dict,
    initial_capital: float = 1_000_000,
    rps_map: dict = None,
    trading_dates: list = None,
) -> dict:
    """完整组合模拟

    参数:
        signals_df: 回测信号DataFrame (需有 date, code, total_score, score_ml, max_score 列)
        stock_daily: {code: pd.DataFrame} 所有股票日线数据
        initial_capital: 初始资金
        rps_map: {code: rps_rank} 每只股票当日RPS排名（用于ML评分重算）
        trading_dates: 全量交易日列表（如提供则用此迭代，否则仅用信号日）

    返回:
        {
            "final_value": float,
            "total_return": float,
            "ann_return": float,
            "max_drawdown": float,
            "sharpe": float,
            "win_rate": float,
            "profit_ratio": float,
            "total_trades": int,
            "exit_reasons": dict,
            "monthly_returns": list,
            "daily_nav": list,
            "closed_trades": list,
        }
    """
    if signals_df.empty:
        return {"total_trades": 0, "final_value": initial_capital, "total_return": 0}

    signals_df = signals_df.copy()
    signals_df["date"] = signals_df["date"].astype(str)

    signals = signals_df.to_dict("records")
    signal_lookup = _build_signal_lookup(signals)

    # 交易日：优先使用传入的全量交易日，否则用信号日
    if trading_dates is not None and len(trading_dates) > 0:
        all_dates = sorted(trading_dates)
    else:
        all_dates = sorted(signal_lookup.keys())
    if not all_dates:
        return {"total_trades": 0}

    # 持仓
    positions = {}  # {code: Position}
    closed_trades = []
    daily_nav = []

    capital = initial_capital
    portfolio_value = initial_capital

    for date_str in all_dates:
        today_signals_raw = signal_lookup.get(date_str, [])

        # ── 先检查已有持仓退出 ──
        to_close = []
        for code, pos in list(positions.items()):
            stock_df = stock_daily.get(code)
            if stock_df is None or stock_df.empty:
                # 数据不足，强制平仓
                to_close.append((code, pos, "NO_DATA", pos.entry_price))
                continue

            current_bar = _get_row_at_date(stock_df, date_str)
            if current_bar is None:
                continue

            # 检查exit check频率
            if pos.held_days % EXIT_CHECK_FREQ != 0 and pos.held_days > 0:
                # 非检查日，只更新价格
                pos.current_price = current_bar["close"]
                if pos.current_price > pos.highest_price:
                    pos.highest_price = pos.current_price
                pos.held_days += 1
                continue

            # 使用入场时评分做退出判断（避免每日重算未来偏差）
            exit_reason = check_exit_rules(pos, current_bar, date_str, None, stock_df=stock_df)
            if exit_reason:
                to_close.append((code, pos, exit_reason, pos.exit_price))

        # 执行平仓
        for code, pos, reason, exit_price in to_close:
            ret = (exit_price / pos.entry_price - 1) * 100 if pos.entry_price > 0 else 0
            trade_pnl = pos.position_size * ret / 100
            capital += pos.position_size + trade_pnl

            closed_trades.append({
                "code": code,
                "entry_date": pos.entry_date,
                "exit_date": date_str,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "position_size": pos.position_size,
                "return_pct": round(ret, 2),
                "pnl": round(trade_pnl, 2),
                "exit_reason": reason,
                "entry_score_ml": pos.entry_score_ml,
                "entry_total_score": pos.entry_total_score,
                "held_days": pos.held_days,
            })
            del positions[code]

        # ── 处理当日新信号 ──
        if today_signals_raw:
            # 过滤已在持仓中的代码（不重复买入同一只）
            new_signals = [s for s in today_signals_raw if s["code"] not in positions]

            if new_signals:
                # 分配仓位
                sized_signals = calc_all_positions(new_signals, capital)
                for sig in sized_signals:
                    code = sig["code"]
                    stock_df = stock_daily.get(code)
                    if stock_df is None:
                        continue

                    entry_price = _get_price_at_date(stock_df, date_str)
                    if entry_price <= 0:
                        continue

                    pos_size = sig["position_size"]
                    # 实际投入金额不能超过可用资金
                    pos_size = min(pos_size, capital * 0.95)

                    if pos_size <= 0:
                        continue

                    capital -= pos_size

                    position = Position(sig, entry_price, pos_size, len(closed_trades))
                    
                    # Phase 4: 计算入场时ATR用于动态止损
                    if ATR_STOP_ENABLED:
                        position.atr_value = _calc_atr(stock_df, period=ATR_PERIOD)
                    
                    positions[code] = position

        # ── 每日净值 ──
        pos_values = sum(p.position_size for p in positions.values())
        portfolio_value = capital + pos_values
        daily_nav.append({"date": date_str, "nav": round(portfolio_value, 2)})

    # ── 强制平仓剩余持仓 ──
    for code, pos in list(positions.items()):
        exit_price = pos.current_price
        ret = (exit_price / pos.entry_price - 1) * 100 if pos.entry_price > 0 else 0
        trade_pnl = pos.position_size * ret / 100
        capital += pos.position_size + trade_pnl
        closed_trades.append({
            "code": code,
            "entry_date": pos.entry_date,
            "exit_date": daily_nav[-1]["date"] if daily_nav else "UNKNOWN",
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "position_size": pos.position_size,
            "return_pct": round(ret, 2),
            "pnl": round(trade_pnl, 2),
            "exit_reason": "FORCED_CLOSE",
            "entry_score_ml": pos.entry_score_ml,
            "entry_total_score": pos.entry_total_score,
            "held_days": pos.held_days,
        })

    final_value = capital
    total_return = (final_value / initial_capital - 1) * 100

    # ── 计算回测指标 ──
    nav_series = pd.Series([d["nav"] for d in daily_nav])

    # 年化收益
    n_days = len(daily_nav)
    ann_return = (final_value / initial_capital) ** (250 / max(n_days, 1)) - 1 if n_days > 0 else 0

    # 最大回撤
    peak = nav_series.cummax()
    drawdowns = (nav_series - peak) / peak * 100
    max_dd = drawdowns.min() if len(drawdowns) > 0 else 0

    # 夏普比
    daily_returns = nav_series.pct_change().dropna()
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() - 0.02 / 250) / daily_returns.std() * np.sqrt(250)
    else:
        sharpe = 0

    # 胜率 & 盈亏比
    closed_df = pd.DataFrame(closed_trades)
    if not closed_df.empty:
        wins = closed_df[closed_df["return_pct"] > 0]
        losses = closed_df[closed_df["return_pct"] <= 0]
        win_rate = len(wins) / len(closed_df) * 100 if len(closed_df) > 0 else 0
        avg_win = wins["return_pct"].mean() if len(wins) > 0 else 0
        avg_loss = abs(losses["return_pct"].mean()) if len(losses) > 0 else 0
        profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses["pnl"].sum() != 0 else float("inf")
    else:
        win_rate = profit_ratio = profit_factor = 0

    # 退出原因分布
    exit_reasons = {}
    for trade in closed_trades:
        reason = trade["exit_reason"]
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # 月度收益
    monthly = []
    if len(closed_trades) > 0:
        trades_df = pd.DataFrame(closed_trades)
        trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])
        trades_df["month"] = trades_df["exit_date"].dt.month
        monthly = trades_df.groupby("month")["return_pct"].mean().to_dict()

    return {
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "ann_return": round(ann_return * 100, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "win_rate": round(win_rate, 1),
        "profit_ratio": round(profit_ratio, 2),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(closed_trades),
        "exit_reasons": exit_reasons,
        "monthly_returns": monthly,
        "closed_trades": closed_trades,
        "daily_nav": daily_nav,
        "final_capital": round(capital, 2),
}


# ─────────────────────────────────────────────
# D. PureSignalSimulator - 无止损纯信号组合模拟
# ─────────────────────────────────────────────


def simulate_pure_portfolio(
    signals_df: pd.DataFrame,
    stock_daily: dict,
    initial_capital: float = 1_000_000,
    trading_dates: list = None,
    hold_days: int = 10,
    dynamic_hold: bool = False,
    market_state: str = None,
    use_price_stop: bool = False,
    use_partial_take_profit: bool = False,
    use_trailing_stop: bool = False,
) -> dict:
    """纯信号模式组合模拟（V1.0 核心）

    V1.0: 新增 dynamic_hold 参数，根据ML评分动态分配持有天数
    增强版: 新增 market_state 市场状态感知 + 纯价格止损 + 部分止盈

    买入信号出现时按仓位规则买入，持有动态天数后卖出。
    无跟踪止损、硬止损、ATR止损——仅保留价格底线止损防止黑天鹅。

    参数:
        signals_df: 回测信号DataFrame
        stock_daily: {code: pd.DataFrame}
        initial_capital: 初始资金
        trading_dates: 全量交易日列表
        hold_days: 固定持有天数（dynamic_hold=False时使用）
        dynamic_hold: 是否启用动态持有期
        market_state: 市场状态 "strong"/"choppy"/"weak"/None
        use_price_stop: 是否启用纯价格止损
        use_partial_take_profit: 是否启用部分止盈
        use_trailing_stop: 是否启用移动止盈（从最高点回落平仓）

    返回:
        与 simulate_portfolio 相同结构的dict
    """
    if signals_df.empty:
        return {"total_trades": 0, "final_value": initial_capital, "total_return": 0}

    signals_df = signals_df.copy()
    signals_df["date"] = signals_df["date"].astype(str)

    signals = signals_df.to_dict("records")
    signal_lookup = _build_signal_lookup(signals)

    # 交易日
    if trading_dates is not None and len(trading_dates) > 0:
        all_dates = sorted(trading_dates)
    else:
        all_dates = sorted(signal_lookup.keys())
    if not all_dates:
        return {"total_trades": 0}

    # 活跃持仓: {code: {entry_date, entry_price, position_size, score_ml, exit_date, shares}}
    active = {}
    closed_trades = []
    daily_nav = []

    capital = initial_capital

    # 预先计算每个信号的退出日期（持有hold_days个交易日）
    date_to_idx = {d: i for i, d in enumerate(all_dates)}

    for date_str in all_dates:
        today_signals_raw = signal_lookup.get(date_str, [])
        today_idx = date_to_idx.get(date_str)

        # ── 检查持仓：价格止损 / 部分止盈 / 到期 ──
        to_close = []
        for code, pos in list(active.items()):
            stock_df = stock_daily.get(code)
            if stock_df is None or stock_df.empty:
                exit_price = pos["entry_price"]
                to_close.append((code, pos, exit_price, date_str, "NO_DATA"))
                continue
            current_price = _get_price_at_date(stock_df, date_str)
            if current_price <= 0:
                current_price = pos["entry_price"]

            # 更新持仓期间最高价（用于止损判断）
            if current_price > pos.get("peak_price", pos["entry_price"]):
                pos["peak_price"] = current_price

            ret = (current_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0

            # Phase 5: 部分止盈（仅触发一次）
            if use_partial_take_profit and not pos.get("partially_sold", False) and ret >= TAKE_PROFIT_TRIGGER:
                reduce_size = pos["position_size"] * TAKE_PROFIT_SELL_RATIO
                trade_pnl = reduce_size * ret / 100
                capital += reduce_size + trade_pnl
                pos["position_size"] -= reduce_size
                pos["partially_sold"] = True
                held = (today_idx - date_to_idx.get(pos["entry_date"], today_idx)) if today_idx is not None else 0
                closed_trades.append({
                    "code": code, "entry_date": pos["entry_date"],
                    "exit_date": date_str, "entry_price": pos["entry_price"],
                    "exit_price": current_price, "position_size": reduce_size,
                    "return_pct": round(ret, 2), "pnl": round(trade_pnl, 2),
                    "exit_reason": "PARTIAL_TP",
                    "entry_score_ml": pos["entry_score_ml"],
                    "entry_total_score": pos["entry_total_score"],
                    "held_days": held,
                })

            # Phase 5: 纯价格止损（防止黑天鹅）
            if use_price_stop and ret <= PURE_STOP_LOSS_PCT:
                trade_pnl = pos["position_size"] * ret / 100
                capital += pos["position_size"] + trade_pnl
                held = (today_idx - date_to_idx.get(pos["entry_date"], today_idx)) if today_idx is not None else 0
                closed_trades.append({
                    "code": code, "entry_date": pos["entry_date"],
                    "exit_date": date_str, "entry_price": pos["entry_price"],
                    "exit_price": current_price, "position_size": pos["position_size"],
                    "return_pct": round(ret, 2), "pnl": round(trade_pnl, 2),
                    "exit_reason": "PRICE_STOP",
                    "entry_score_ml": pos["entry_score_ml"],
                    "entry_total_score": pos["entry_total_score"],
                    "held_days": held,
                })
                del active[code]
                continue

            # Phase 6: 移动止盈（从最高点回落平仓）
            if use_trailing_stop and TRAILING_STOP_ENABLED:
                profit_pct = (pos["peak_price"] / pos["entry_price"] - 1) * 100
                drawdown_pct = (current_price / pos["peak_price"] - 1) * 100
                if profit_pct >= TRAILING_STOP_ACTIVATE_AT and drawdown_pct <= TRAILING_STOP_FROM_PEAK_PCT:
                    trade_pnl = pos["position_size"] * ret / 100
                    capital += pos["position_size"] + trade_pnl
                    held = (today_idx - date_to_idx.get(pos["entry_date"], today_idx)) if today_idx is not None else 0
                    closed_trades.append({
                        "code": code, "entry_date": pos["entry_date"],
                        "exit_date": date_str, "entry_price": pos["entry_price"],
                        "exit_price": current_price, "position_size": pos["position_size"],
                        "return_pct": round(ret, 2), "pnl": round(trade_pnl, 2),
                        "exit_reason": "TRAILING_STOP",
                        "entry_score_ml": pos["entry_score_ml"],
                        "entry_total_score": pos["entry_total_score"],
                        "held_days": held,
                    })
                    del active[code]
                    continue

            # Phase 4: 到期卖出
            exit_idx = pos["exit_idx"]
            if today_idx is not None and today_idx >= exit_idx:
                to_close.append((code, pos, current_price, date_str, "PURE_HOLD"))

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
                "entry_score_ml": pos["entry_score_ml"],
                "entry_total_score": pos["entry_total_score"],
                "held_days": pos["held_days"],
            })
            del active[code]

        # ── 处理当日新信号 ──
        if today_signals_raw:
            new_signals = [s for s in today_signals_raw if s["code"] not in active]
            if new_signals:
                sized_signals = calc_all_positions(new_signals, capital)
                for sig in sized_signals:
                    code = sig["code"]
                    if code in active:
                        continue
                    stock_df = stock_daily.get(code)
                    if stock_df is None:
                        continue
                    entry_price = _get_price_at_date(stock_df, date_str)
                    if entry_price <= 0:
                        continue
                    pos_size = sig["position_size"]
                    pos_size = min(pos_size, capital * 0.95)
                    
                    # Phase 5: 市场状态仓位乘数
                    if market_state is not None:
                        from src.market_state import get_position_multiplier
                        pos_size *= get_position_multiplier(market_state)
                    
                    if pos_size <= 0:
                        continue

                    capital -= pos_size

                    # Phase 5: 动态持有期（精细分档）
                    if dynamic_hold:
                        from src.market_state import get_hold_days
                        actual_hold = get_hold_days(sig.get("score_ml", 0))
                    else:
                        actual_hold = hold_days

                    # 计算退出索引
                    exit_idx = today_idx + actual_hold if today_idx is not None else None

                    active[code] = {
                        "entry_date": date_str,
                        "entry_price": entry_price,
                        "position_size": pos_size,
                        "entry_score_ml": sig.get("score_ml", 0),
                        "entry_total_score": sig.get("total_score", 0),
                        "exit_idx": exit_idx,
                        "held_days": actual_hold,
                        "peak_price": entry_price,
                        "partially_sold": False,
                    }

        # ── 每日净值 ──
        pos_values = sum(p["position_size"] for p in active.values())
        portfolio_value = capital + pos_values
        daily_nav.append({"date": date_str, "nav": round(portfolio_value, 2)})

    # ── 强制平仓剩余持仓 ──
    for code, pos in list(active.items()):
        stock_df = stock_daily.get(code)
        if stock_df is not None and not stock_df.empty:
            exit_price = _get_price_at_date(stock_df, daily_nav[-1]["date"])
        else:
            exit_price = pos["entry_price"]
        if exit_price <= 0:
            exit_price = pos["entry_price"]
        ret = (exit_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0
        trade_pnl = pos["position_size"] * ret / 100
        capital += pos["position_size"] + trade_pnl
        closed_trades.append({
            "code": code,
            "entry_date": pos["entry_date"],
            "exit_date": daily_nav[-1]["date"] if daily_nav else "UNKNOWN",
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "position_size": pos["position_size"],
            "return_pct": round(ret, 2),
            "pnl": round(trade_pnl, 2),
            "exit_reason": "FORCED_CLOSE",
            "entry_score_ml": pos["entry_score_ml"],
            "entry_total_score": pos["entry_total_score"],
            "held_days": pos["held_days"],
        })

    final_value = capital
    total_return = (final_value / initial_capital - 1) * 100

    # ── 计算指标 ──
    nav_series = pd.Series([d["nav"] for d in daily_nav])
    n_days = len(daily_nav)
    ann_return = (final_value / initial_capital) ** (250 / max(n_days, 1)) - 1 if n_days > 0 else 0

    peak = nav_series.cummax()
    drawdowns = (nav_series - peak) / peak * 100
    max_dd = drawdowns.min() if len(drawdowns) > 0 else 0

    daily_returns = nav_series.pct_change().dropna()
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() - 0.02 / 250) / daily_returns.std() * np.sqrt(250)
    else:
        sharpe = 0

    closed_df = pd.DataFrame(closed_trades)
    if not closed_df.empty:
        wins = closed_df[closed_df["return_pct"] > 0]
        losses = closed_df[closed_df["return_pct"] <= 0]
        win_rate = len(wins) / len(closed_df) * 100 if len(closed_df) > 0 else 0
        avg_win = wins["return_pct"].mean() if len(wins) > 0 else 0
        avg_loss = abs(losses["return_pct"].mean()) if len(losses) > 0 else 0
        profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses["pnl"].sum() != 0 else float("inf")
    else:
        win_rate = profit_ratio = profit_factor = 0

    exit_reasons = {}
    for trade in closed_trades:
        reason = trade["exit_reason"]
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    monthly = []
    if len(closed_trades) > 0:
        trades_df = pd.DataFrame(closed_trades)
        trades_df["exit_date"] = pd.to_datetime(trades_df["exit_date"])
        trades_df["month"] = trades_df["exit_date"].dt.month
        monthly = trades_df.groupby("month")["return_pct"].mean().to_dict()

    return {
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "ann_return": round(ann_return * 100, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "win_rate": round(win_rate, 1),
        "profit_ratio": round(profit_ratio, 2),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(closed_trades),
        "exit_reasons": exit_reasons,
        "monthly_returns": monthly,
        "closed_trades": closed_trades,
        "daily_nav": daily_nav,
        "final_capital": round(capital, 2),
    }
