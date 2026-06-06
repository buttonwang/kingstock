"""股票评分排序系统 - 6维加权评分

为最终股票池中的每只股票计算综合评分，用于排序和优先级推荐。

评分维度：
1. MACD信号强度  (0~30)  - DIF>DEA差值越大、金叉质量越高，分数越高
2. ZJTJ控盘强度  (0~20)  - 控盘度值越大、增长趋势越强，分数越高
3. KDJ信号质量  (0~15)  - K在20~30区域金叉加分，J<0刚转正加分
4. RPS板块排名  (0~15)  - 板块RPS排名越靠前分数越高
5. 成交量确认   (0~10)  - 放量倍数换算
6. 财务增长     (0~10)  - CAGR按档次换算
"""
import pandas as pd
import numpy as np

from config.settings import (
    SCORE_MACD_MAX, SCORE_ZJTJ_MAX, SCORE_KDJ_MAX,
    SCORE_RPS_MAX, SCORE_VOLUME_MAX, SCORE_FINANCE_MAX, SCORE_ML_MAX,
    SCORE_DIF_DEA_GAP_THRESHOLD, SCORE_KONGPAN_THRESHOLD, ML_SCORE_ENABLED,
    PROFIT_GROWTH_MIN,
)
from src.indicators.enhanced_rules import check_volume_confirmation


def score_macd(df: pd.DataFrame) -> int:
    """计算MACD信号强度分 (0~SCORE_MACD_MAX)

    评分逻辑：
    - DIF>DEA且均为正 (金叉状态): 按DIF-DEA差值线性打分
    - 差值 >= threshold: 满分
    - DIF>0但DEA>0且DIF<=DEA (即将金叉): 半档分
    """
    if len(df) < 2:
        return 0

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    dif, dea = today.get('dif'), today.get('dea')
    if pd.isna(dif) or pd.isna(dea):
        return 0

    if dif > 0 and dea > 0 and dif > dea:
        gap = abs(dif - dea)
        if gap >= SCORE_DIF_DEA_GAP_THRESHOLD:
            return SCORE_MACD_MAX
        return int(SCORE_MACD_MAX * (gap / SCORE_DIF_DEA_GAP_THRESHOLD))

    # MACD柱由绿转红算半个信号
    macd_val = today.get('macd', 0)
    macd_prev = yesterday.get('macd', 0)
    if not pd.isna(macd_val) and not pd.isna(macd_prev):
        if macd_prev < 0 and macd_val >= 0:
            return int(SCORE_MACD_MAX * 0.5)

    return 0


def score_zjtj(df: pd.DataFrame) -> int:
    """计算ZJTJ控盘强度分 (0~SCORE_ZJTJ_MAX，V2 增强版)

    V2 评分逻辑（按控盘强度分级）：
    - 强控盘 (kongpan >= 1.0)：满分
    - 中控盘且增加 (0.5~1.0，环比上升)：按比例线性打分
    - 中控盘但不再增加：半档分
    - 弱控盘 (0~0.5)：按比例打折
    - 无控盘：0分
    """
    from src.indicators.zjtj import get_kongpan_level

    if len(df) < 2:
        return 0

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    kongpan = today.get('kongpan')
    prev_kongpan = yesterday.get('kongpan')

    if pd.isna(kongpan) or kongpan <= 0:
        return 0

    level = get_kongpan_level(kongpan)

    if level == "strong":
        # 强控盘满分
        return SCORE_ZJTJ_MAX

    elif level == "medium":
        if kongpan > prev_kongpan:  # 控盘在增加
            if kongpan >= SCORE_KONGPAN_THRESHOLD:
                return SCORE_ZJTJ_MAX
            return int(SCORE_ZJTJ_MAX * (kongpan / SCORE_KONGPAN_THRESHOLD))
        else:  # 中控盘但不再增加
            return int(SCORE_ZJTJ_MAX * 0.5)

    elif level == "weak":
        # 弱控盘：基础分
        if kongpan > prev_kongpan:
            return int(SCORE_ZJTJ_MAX * 0.3)
        return int(SCORE_ZJTJ_MAX * 0.15)

    return 0


def score_kdj(df: pd.DataFrame) -> int:
    """计算KDJ信号质量分 (0~SCORE_KDJ_MAX)

    评分逻辑：
    - K在20~30向上金叉D: 高分 (趋势底部确认)
    - J从负转正: 中等分 (超卖反弹)
    - K>D且J>0 (宽松信号): 基础分
    """
    max_score = SCORE_KDJ_MAX
    if len(df) < 2:
        return 0

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    k, d, j = today.get('k'), today.get('d'), today.get('j')
    k_prev, d_prev, j_prev = yesterday.get('k'), yesterday.get('d'), yesterday.get('j')

    if any(pd.isna(x) for x in [k, d, j, k_prev, d_prev, j_prev]):
        return 0

    # 条件1: K在20~30区间，今日K>D且昨日K<=D → 底部金叉，满分
    cond1 = (20 <= k <= 35 and k > d and k_prev <= d_prev)
    if cond1:
        return max_score

    # 条件2: J从负转正 → 超卖反弹，中等分
    cond2 = (j_prev < 0 and j >= 0)
    if cond2:
        return int(max_score * 0.7)

    # 条件3: K>D且J>0 → 多头状态，基础分
    cond3 = (k > d and j > 0)
    if cond3:
        return int(max_score * 0.4)

    return 0


def score_rps(rps_rank: int, top_n: int = 20) -> int:
    """根据板块RPS排名计算评分 (0~SCORE_RPS_MAX)

    排名越靠前分数越高，线性递减。
    如排名1得满分，排名20得1分。
    """
    if rps_rank <= 0:
        return 0
    if rps_rank > top_n:
        return 0

    # 线性递减：rank=1 满分，rank=top_n 得1分
    score = SCORE_RPS_MAX * (1 - (rps_rank - 1) / top_n)
    return max(1, int(round(score)))


def score_volume(df: pd.DataFrame) -> int:
    """计算成交量确认分 (0~SCORE_VOLUME_MAX)

    根据放量倍数线性打分。
    """
    if not check_volume_confirmation(df):
        return 0

    return SCORE_VOLUME_MAX


def score_finance(cagr: float) -> int:
    """计算财务增长分 (0~SCORE_FINANCE_MAX)

    CAGR >= 40%: 满分
    CAGR >= 30%: 80%
    CAGR >= 20%: 60%
    CAGR >= 10%: 40%
    其他: 0
    """
    if cagr >= 0.40:
        return SCORE_FINANCE_MAX
    elif cagr >= 0.30:
        return int(SCORE_FINANCE_MAX * 0.8)
    elif cagr >= 0.20:
        return int(SCORE_FINANCE_MAX * 0.6)
    elif cagr >= 0.10:
        return int(SCORE_FINANCE_MAX * 0.4)
    return 0


def compute_total_score(
    df_macd: pd.DataFrame,
    df_kdj: pd.DataFrame,
    df_zjtj: pd.DataFrame,
    rps_rank: int = 0,
    cagr: float = 0.0,
    top_n: int = 20,
    ml_score: float = None,
) -> dict:
    """计算一只股票的综合评分 (7维, 含ML)

    参数:
        df_macd: 已计算MACD的日线DataFrame
        df_kdj: 已计算KDJ的日线DataFrame
        df_zjtj: 已计算ZJTJ的日线DataFrame
        rps_rank: 所属板块RPS排名
        cagr: 近N年净利润复合增长率
        top_n: RPS前N板块
        ml_score: XGBoost ML评分 (None=不启用)

    返回:
        {
            "score_macd": int,
            "score_zjtj": int,
            "score_kdj": int,
            "score_rps": int,
            "score_volume": int,
            "score_finance": int,
            "score_ml": int,           # ML评分（新增）
            "total_score": int,
            "max_score": int,
        }
    """
    sm = score_macd(df_macd)
    sz = score_zjtj(df_zjtj)
    sk = score_kdj(df_kdj)
    sr = score_rps(rps_rank, top_n)
    sv = score_volume(df_macd)  # 成交量从日线获取
    sf = score_finance(cagr)

    # ML评分
    sml = 0
    if ml_score is not None and ML_SCORE_ENABLED:
        sml = min(int(round(ml_score)), SCORE_ML_MAX)

    total = sm + sz + sk + sr + sv + sf + sml
    max_total = SCORE_MACD_MAX + SCORE_ZJTJ_MAX + SCORE_KDJ_MAX + \
                SCORE_RPS_MAX + SCORE_VOLUME_MAX + SCORE_FINANCE_MAX + \
                (SCORE_ML_MAX if ML_SCORE_ENABLED else 0)

    return {
        "score_macd": sm,
        "score_zjtj": sz,
        "score_kdj": sk,
        "score_rps": sr,
        "score_volume": sv,
        "score_finance": sf,
        "score_ml": sml,
        "total_score": total,
        "max_score": max_total,
    }
