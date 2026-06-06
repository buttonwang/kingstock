"""KDJ指标计算模块

严格按照通达信公式实现：
RSV = (CLOSE - LLV(LOW, N)) / (HHV(HIGH, N) - LLV(LOW, N)) * 100
K = SMA(RSV, M1, 1)
D = SMA(K, M2, 1)
J = 3*K - 2*D

通达信SMA递推算法：SMA(X, N, M) = (M*X + (N-M)*REF(SMA,1)) / N，初始值取X的第一个有效值
"""
import pandas as pd
import numpy as np
from config.settings import KDJ_N, KDJ_M1, KDJ_M2


def tdx_sma(series: pd.Series, n: int, m: int) -> pd.Series:
    """通达信SMA递推算法
    SMA(X, N, M) = (M*X + (N-M)*前一日SMA) / N
    初始值取X的第一个有效值（非NaN）
    """
    result = np.full(len(series), np.nan, dtype=float)
    values = series.values

    # 找到第一个有效值作为初始值
    first_valid = -1
    for i in range(len(values)):
        if not np.isnan(values[i]):
            first_valid = i
            result[i] = values[i]
            break

    if first_valid == -1:
        return pd.Series(result, index=series.index)

    # 从第一个有效值的下一个开始递推
    for i in range(first_valid + 1, len(values)):
        if np.isnan(values[i]):
            result[i] = result[i - 1]  # 当前值为NaN时，沿用前一日SMA
        else:
            result[i] = (m * values[i] + (n - m) * result[i - 1]) / n

    return pd.Series(result, index=series.index)


def calculate_kdj(df: pd.DataFrame, n=KDJ_N, m1=KDJ_M1, m2=KDJ_M2) -> pd.DataFrame:
    """计算KDJ指标
    输入: df必须包含'high','low','close'列，按日期升序
    输出: 添加 rsv, k, d, j 四列
    """
    close = df['close']
    high = df['high']
    low = df['low']

    # LLV(LOW, N) 和 HHV(HIGH, N)
    llv_low = low.rolling(window=n, min_periods=1).min()
    hhv_high = high.rolling(window=n, min_periods=1).max()

    # RSV = (CLOSE - LLV(LOW, N)) / (HHV(HIGH, N) - LLV(LOW, N)) * 100
    denom = hhv_high - llv_low
    rsv = np.where(denom == 0, 0, (close - llv_low) / denom * 100)
    rsv = pd.Series(rsv, index=df.index)

    # K = SMA(RSV, M1, 1)
    k = tdx_sma(rsv, m1, 1)
    # D = SMA(K, M2, 1)
    d = tdx_sma(k, m2, 1)
    # J = 3*K - 2*D
    j = 3 * k - 2 * d

    df = df.copy()
    df['rsv'] = rsv
    df['k'] = k
    df['d'] = d
    df['j'] = j

    return df


def is_kdj_buy_signal(df: pd.DataFrame) -> bool:
    """判断最新一天是否为KDJ买入信号
    买入条件（满足任一）：
    1. K在20左右向上交叉D（K<30 且 今日K>D 且 昨日K<=D）
    2. J从负值转正（昨日J<0，今日J>=0）
    """
    if len(df) < 2:
        return False

    # 需要的列不存在则先计算
    if 'k' not in df.columns or 'd' not in df.columns or 'j' not in df.columns:
        df = calculate_kdj(df)

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    # 检查是否有NaN
    if pd.isna(today['k']) or pd.isna(today['d']) or pd.isna(today['j']):
        return False
    if pd.isna(yesterday['k']) or pd.isna(yesterday['d']) or pd.isna(yesterday['j']):
        return False

    # 条件1: K在20左右向上交叉D（K<30 且 今日K>D 且 昨日K<=D）
    cond1 = (today['k'] < 30
             and today['k'] > today['d']
             and yesterday['k'] <= yesterday['d'])

    # 条件2: J从负值转正
    cond2 = yesterday['j'] < 0 and today['j'] >= 0

    return cond1 or cond2


def is_kdj_bullish(df: pd.DataFrame) -> bool:
    """判断最新一天是否处于KDJ多头趋势状态（宽松条件）

    适用于手工选股等场景，不要求低位金叉，只要求：
    K > D 且 J > 0（多头排列且未超卖反转）
    """
    if len(df) < 2:
        return False

    if 'k' not in df.columns or 'd' not in df.columns or 'j' not in df.columns:
        df = calculate_kdj(df)

    today = df.iloc[-1]

    if pd.isna(today['k']) or pd.isna(today['d']) or pd.isna(today['j']):
        return False

    return today['k'] > today['d'] and today['j'] > 0
