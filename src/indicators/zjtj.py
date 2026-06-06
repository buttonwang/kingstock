"""庄家控盘ZJTJ指标计算模块（V2 增强版）

严格按照通达信公式实现：
VAR1 = EMA(EMA(CLOSE, 9), 9)
控盘 = (VAR1 - REF(VAR1, 1)) / REF(VAR1, 1) * 1000

V2 增强：
- 控盘度分级：强控盘/中控盘/弱控盘
- 控盘趋势检查：过去N日控盘度呈上升趋势比例
"""
import pandas as pd
import numpy as np

from config.settings import (
    KONGPAN_STRONG, KONGPAN_MEDIUM, KONGPAN_WEAK,
    KONGPAN_TREND_DAYS, KONGPAN_TREND_MIN_PCT, KONGPAN_WEAK_BUY,
)


def calculate_zjtj(df: pd.DataFrame) -> pd.DataFrame:
    """计算庄家控盘ZJTJ指标
    输入: df必须包含'close'列，按日期升序
    输出: 添加 var1, kongpan(控盘度) 列
    """
    close = df['close']

    # VAR1 = EMA(EMA(CLOSE, 9), 9)
    ema1 = close.ewm(span=9, adjust=False).mean()
    var1 = ema1.ewm(span=9, adjust=False).mean()

    # 控盘 = (VAR1 - REF(VAR1, 1)) / REF(VAR1, 1) * 1000
    var1_prev = var1.shift(1)
    kongpan = np.where(var1_prev == 0, 0, (var1 - var1_prev) / var1_prev * 1000)
    kongpan = pd.Series(kongpan, index=df.index)

    df = df.copy()
    df['var1'] = var1
    df['kongpan'] = kongpan

    return df


def get_kongpan_level(kongpan: float) -> str:
    """判断控盘强度等级

    返回: "strong" / "medium" / "weak" / "none"
    """
    if kongpan >= KONGPAN_STRONG:
        return "strong"
    elif kongpan >= KONGPAN_MEDIUM:
        return "medium"
    elif kongpan > KONGPAN_WEAK:
        return "weak"
    else:
        return "none"


def check_kongpan_trend(df: pd.DataFrame, days: int = None,
                        min_pct: float = None) -> bool:
    """检查过去N日控盘度是否呈上升趋势

    计算逻辑：统计过去days天中，控盘度环比上升的天数占比

    返回: True 表示趋势达标（上升天数 >= days * min_pct）
    """
    if 'kongpan' not in df.columns:
        df = calculate_zjtj(df)

    days = days or KONGPAN_TREND_DAYS
    min_pct = min_pct or KONGPAN_TREND_MIN_PCT

    if len(df) < days + 1:
        return False

    recent = df['kongpan'].iloc[-(days + 1):].values
    if len(recent) < 2:
        return False

    # 统计环比上升的天数
    up_count = 0
    for i in range(1, len(recent)):
        if not pd.isna(recent[i]) and not pd.isna(recent[i - 1]):
            if recent[i] > recent[i - 1]:
                up_count += 1

    ratio = up_count / (len(recent) - 1) * 100
    return ratio >= min_pct


def is_zjtj_buy_signal(df: pd.DataFrame) -> bool:
    """判断最新一天是否为有庄控盘信号（V2 增强版）

    V2 增强逻辑：
    1. 强控盘（kongpan >= 1.0）：直接买入
    2. 中控盘（0.5 ~ 1.0）：需控盘 > 前一日 且 控盘趋势达标
    3. 弱控盘（0 ~ 0.5）：仅当 KONGPAN_WEAK_BUY=True 且趋势达标才买入
    """
    if len(df) < 2:
        return False

    # 需要的列不存在则先计算
    if 'kongpan' not in df.columns:
        df = calculate_zjtj(df)

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    # 检查是否有NaN
    if pd.isna(today['kongpan']) or pd.isna(yesterday['kongpan']):
        return False

    kongpan = today['kongpan']
    level = get_kongpan_level(kongpan)

    if level == "none":
        return False

    if level == "strong":
        # 强控盘直接买入（控盘强度足够，无需额外条件）
        return True

    elif level == "medium":
        # 中控盘：控盘需在增加 + 趋势达标
        if kongpan <= yesterday['kongpan']:
            return False
        return check_kongpan_trend(df)

    elif level == "weak":
        # 弱控盘：仅当配置允许 + 控盘增加 + 趋势达标
        if not KONGPAN_WEAK_BUY:
            return False
        if kongpan <= yesterday['kongpan']:
            return False
        return check_kongpan_trend(df)

    return False
