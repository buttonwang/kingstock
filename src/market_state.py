"""市场状态检测模块

基于全市场股票平均表现判断当前市场状态。
用于纯信号交易系统的入场过滤和仓位调整。

用法:
    state = get_market_state(bench_10d)
    if is_tradeable(state):
        # 正常交易
"""

from config.settings import MARKET_STRONG_THRESHOLD, MARKET_WEAK_THRESHOLD

# 市场状态常量
STRONG = "strong"        # 强势市场 — 正常交易
CHOPPY = "choppy"        # 震荡市场 — 减仓+收紧过滤
WEAK = "weak"            # 弱势市场 — 不交易


def get_market_state(bench_10d: float) -> str:
    """基于全市场平均10日涨幅判断市场状态

    参数:
        bench_10d: 全市场股票10日平均涨幅(%)
            - 正值表示市场整体上涨
            - 负值表示市场整体下跌

    返回:
        "strong" / "choppy" / "weak"
    """
    if bench_10d > MARKET_STRONG_THRESHOLD:
        return STRONG
    elif bench_10d < MARKET_WEAK_THRESHOLD:
        return WEAK
    else:
        return CHOPPY


def is_tradeable(state: str) -> bool:
    """判断当前是否可交易"""
    return state != WEAK


def get_score_threshold(state: str) -> int:
    """根据市场状态返回最低总分门槛"""
    from config.settings import (
        SCORE_THRESHOLD_STRONG,
        SCORE_THRESHOLD_CHOPPY,
        SCORE_THRESHOLD_WEAK,
    )
    thresholds = {
        STRONG: SCORE_THRESHOLD_STRONG,
        CHOPPY: SCORE_THRESHOLD_CHOPPY,
        WEAK: SCORE_THRESHOLD_WEAK,
    }
    return thresholds.get(state, SCORE_THRESHOLD_STRONG)


def get_ml_min_threshold(state: str) -> int:
    """根据市场状态返回最低ML评分门槛"""
    from config.settings import ML_SCORE_MIN_THRESHOLD

    if state == STRONG:
        return ML_SCORE_MIN_THRESHOLD  # 10
    elif state == CHOPPY:
        return ML_SCORE_MIN_THRESHOLD + 1  # 11（收紧）
    else:
        return 999  # 不交易


def get_max_daily_signals(state: str) -> int:
    """根据市场状态返回每日最大信号数"""
    from config.settings import MAX_DAILY_STRONG, MAX_DAILY_CHOPPY, MAX_DAILY_WEAK

    limits = {
        STRONG: MAX_DAILY_STRONG,
        CHOPPY: MAX_DAILY_CHOPPY,
        WEAK: MAX_DAILY_WEAK,
    }
    return limits.get(state, 0)


def get_position_multiplier(state: str) -> float:
    """根据市场状态返回仓位乘数"""
    from config.settings import POSITION_STRONG_MULT, POSITION_CHOPPY_MULT

    multipliers = {
        STRONG: POSITION_STRONG_MULT,
        CHOPPY: POSITION_CHOPPY_MULT,
        WEAK: 0.0,  # 弱势不交易，仓位为0
    }
    return multipliers.get(state, 1.0)


def get_hold_days(ml_score: float) -> int:
    """根据ML评分返回动态持有天数

    参数:
        ml_score: ML评分 (0-15)

    返回:
        持有天数
    """
    from config.settings import HOLD_ML_13_15, HOLD_ML_11_12, HOLD_ML_10

    if ml_score >= 13:
        return HOLD_ML_13_15  # 7天（Phase 6: 缩短持有期）
    elif ml_score >= 11:
        return HOLD_ML_11_12  # 5天
    elif ml_score >= 10:
        return HOLD_ML_10     # 3天
    return 3  # 兜底
