"""MACD指标计算模块

严格按照通达信公式实现：
DIF = EMA(CLOSE, 12) - EMA(CLOSE, 26)
DEA = EMA(DIF, 9)
MACD = (DIF - DEA) * 2
"""
import pandas as pd
import numpy as np
from config.settings import MACD_SHORT, MACD_LONG, MACD_MID


def calculate_macd(df: pd.DataFrame, short=MACD_SHORT, long=MACD_LONG, mid=MACD_MID) -> pd.DataFrame:
    """计算MACD指标
    输入: df必须包含'close'列，按日期升序排列
    输出: 在df上添加 dif, dea, macd 三列并返回
    """
    close = df['close']

    # 通达信EMA使用 adjust=False，与通达信计算结果一致
    ema_short = close.ewm(span=short, adjust=False).mean()
    ema_long = close.ewm(span=long, adjust=False).mean()

    dif = ema_short - ema_long
    dea = dif.ewm(span=mid, adjust=False).mean()
    macd = (dif - dea) * 2

    df = df.copy()
    df['dif'] = dif
    df['dea'] = dea
    df['macd'] = macd

    return df


def is_macd_buy_signal(df: pd.DataFrame) -> bool:
    """判断最新一天是否为MACD买入信号（V2 增强版）
    买入条件（满足任一）：
    1. DIF > 0 且 DEA > 0，且 DIF向上突破DEA（今日DIF>DEA，昨日DIF<=DEA）
    2. MACD柱由绿变红（前一日MACD<0，今日MACD>=0）且 DIF > 0

    V2 增强：
    - cond1 增加 DIF 环比上升检查（DIF > DIF_prev）
    - cond2 增加 DIF > 0 要求（排除弱势反转）
    """
    if len(df) < 2:
        return False

    # 需要的列不存在则先计算
    if 'dif' not in df.columns or 'dea' not in df.columns or 'macd' not in df.columns:
        df = calculate_macd(df)

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    # 检查是否有NaN
    if pd.isna(today['dif']) or pd.isna(today['dea']) or pd.isna(today['macd']):
        return False
    if pd.isna(yesterday['dif']) or pd.isna(yesterday['dea']) or pd.isna(yesterday['macd']):
        return False

    # 条件1: DIF > 0 且 DEA > 0，且 DIF向上突破DEA，且 DIF 环比上升
    cond1 = (today['dif'] > 0 and today['dea'] > 0
             and today['dif'] > today['dea']
             and yesterday['dif'] <= yesterday['dea']
             and today['dif'] > yesterday['dif'])  # DIF 环比上升

    # 条件2: MACD柱由绿变红 + DIF > 0（排除弱势反转）
    cond2 = (yesterday['macd'] < 0 and today['macd'] >= 0
             and today['dif'] > 0)

    return cond1 or cond2


def is_macd_bullish(df: pd.DataFrame) -> bool:
    """判断最新一天是否处于MACD多头趋势状态（宽松条件）

    适用于手工选股等场景，不要求当日金叉，只要求：
    DIF > 0 且 DEA > 0 且 DIF > DEA 且 MACD > 0
    即处于多头排列且柱状线为正。
    """
    if len(df) < 2:
        return False

    if 'dif' not in df.columns or 'dea' not in df.columns or 'macd' not in df.columns:
        df = calculate_macd(df)

    today = df.iloc[-1]

    if pd.isna(today['dif']) or pd.isna(today['dea']) or pd.isna(today['macd']):
        return False

    # 多头趋势: DIF > 0, DEA > 0, DIF > DEA, MACD > 0
    return (today['dif'] > 0 and today['dea'] > 0
            and today['dif'] > today['dea']
            and today['macd'] > 0)
