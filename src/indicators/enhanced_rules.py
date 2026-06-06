"""增强辅助规则：成交量确认、均线多头排列、价格位置过滤

用于选股和回测中作为补充筛选条件，提升信号质量。
"""
import pandas as pd
import numpy as np

from config.settings import (
    VOLUME_CONFIRM_ENABLED, VOLUME_MA_PERIOD, VOLUME_THRESHOLD,
    MA_ALIGN_ENABLED, MA_SHORT, MA_MID, MA_LONG, MA_EXTRA,
    PRICE_POSITION_ENABLED, PRICE_LOOKBACK, PRICE_LOWER_PCT, PRICE_UPPER_PCT,
)


def check_volume_confirmation(df: pd.DataFrame) -> bool:
    """成交量确认：选股日成交量 > MA均量的倍数阈值

    参数:
        df: 日线DataFrame，需含 'volume' 或 'amount' 列，按日期升序

    返回:
        True 表示成交量满足放量条件
    """
    if not VOLUME_CONFIRM_ENABLED:
        return True

    if df is None or len(df) < VOLUME_MA_PERIOD + 1:
        return False

    # 优先使用 volume 列，如果没有则用 amount（成交额）
    if 'volume' in df.columns:
        vol = df['volume']
        # 如果volume全部为0或NaN，视为数据不可用，跳过检查
        if vol.sum() == 0 or vol.isna().all():
            return True
    elif 'amount' in df.columns:
        vol = df['amount']
        if vol.sum() == 0 or vol.isna().all():
            return True
    else:
        return True  # 无成交量数据时放过

    latest_vol = vol.iloc[-1]
    if pd.isna(latest_vol) or latest_vol == 0:
        return False

    # 计算前N日均量
    ma_vol = vol.iloc[-(VOLUME_MA_PERIOD + 1):-1].mean()
    if pd.isna(ma_vol) or ma_vol == 0:
        return False

    return latest_vol >= ma_vol * VOLUME_THRESHOLD


def check_ma_alignment(df: pd.DataFrame) -> bool:
    """均线多头排列检查：MA_SHORT > MA_MID > MA_LONG > MA_EXTRA

    参数:
        df: 日线DataFrame，需含 'close' 列，按日期升序

    返回:
        True 表示均线呈多头排列
    """
    if not MA_ALIGN_ENABLED:
        return True

    if df is None or len(df) < MA_EXTRA:
        return False

    close = df['close']

    ma_s = close.rolling(window=MA_SHORT).mean().iloc[-1]
    ma_m = close.rolling(window=MA_MID).mean().iloc[-1]
    ma_l = close.rolling(window=MA_LONG).mean().iloc[-1]
    ma_e = close.rolling(window=MA_EXTRA).mean().iloc[-1]

    if any(pd.isna(x) for x in [ma_s, ma_m, ma_l, ma_e]):
        return False

    return ma_s > ma_m > ma_l > ma_e


def check_price_position(df: pd.DataFrame) -> bool:
    """价格位置过滤：最新收盘价在近N日价格区间 [P%, (100-P)%] 分位内

    避免买入处于超买区间（追高）或超跌区间（弱势未确认反转）的股票。

    参数:
        df: 日线DataFrame，需含 'close' 列，按日期升序

    返回:
        True 表示价格在合理区间内
    """
    if not PRICE_POSITION_ENABLED:
        return True

    if df is None or len(df) < PRICE_LOOKBACK:
        return False

    recent_close = df['close'].iloc[-PRICE_LOOKBACK:]
    latest = recent_close.iloc[-1]
    low = recent_close.min()
    high = recent_close.max()

    if pd.isna(latest) or pd.isna(low) or pd.isna(high) or high == low:
        return False

    percent = (latest - low) / (high - low) * 100
    return PRICE_LOWER_PCT <= percent <= PRICE_UPPER_PCT


def check_all_enhanced_rules(df: pd.DataFrame) -> dict:
    """一次性检查所有增强规则

    返回:
        {
            "volume_pass": bool,
            "ma_alignment_pass": bool,
            "price_position_pass": bool,
            "rules_passed": int,    # 通过了几条
            "rules_total": int,     # 总规则数
        }
    """
    volume_pass = check_volume_confirmation(df)
    ma_pass = check_ma_alignment(df)
    price_pass = check_price_position(df)

    return {
        "volume_pass": volume_pass,
        "ma_alignment_pass": ma_pass,
        "price_position_pass": price_pass,
        "rules_passed": sum([volume_pass, ma_pass, price_pass]),
        "rules_total": 3,
    }
