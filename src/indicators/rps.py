"""RPS（相对强度排名）指标计算模块（V2 增强版）

计算板块RPS：根据近period天的累计涨幅排名，反映板块相对强弱。

V2 增强：
- 持久性RPS计算（多周期RPS排名历史）
- RPS持续性检查：板块需持续在排名前N
- 动态TOP_N：根据市场行情调
"""
import pandas as pd
import numpy as np
from config.settings import (
    RPS_PERIOD, RPS_TOP_N,
    RPS_CONSISTENCY_ENABLED, RPS_CONSISTENCY_DAYS, RPS_CONSISTENCY_RANK,
    RPS_DYNAMIC_ENABLED, RPS_DYNAMIC_BASE_N, RPS_DYNAMIC_MIN_N,
    RPS_DYNAMIC_MAX_N, RPS_DYNAMIC_THRESHOLD,
)


def calculate_sector_rps(sector_daily_data: dict, period: int = RPS_PERIOD) -> pd.DataFrame:
    """计算所有板块的RPS值
    输入: sector_daily_data - dict, key为板块名称, value为该板块的日线DataFrame(需含close列)
    逻辑: 计算每个板块近period天的累计涨幅，然后排名
    输出: DataFrame[sector_name, change_pct, rps_rank] 按RPS排名排序
    """
    results = []

    for sector_name, df in sector_daily_data.items():
        if df is None or len(df) < 2 or 'close' not in df.columns:
            continue

        # 取最近period+1个交易日（需要period+1个收盘价来算period天涨幅）
        recent = df.tail(period + 1)

        if len(recent) < 2:
            continue

        start_price = recent.iloc[0]['close']
        end_price = recent.iloc[-1]['close']

        # 防止起始价为0或NaN
        if pd.isna(start_price) or pd.isna(end_price) or start_price == 0:
            continue

        change_pct = (end_price - start_price) / start_price * 100
        results.append({
            'sector_name': sector_name,
            'change_pct': change_pct,
        })

    if not results:
        return pd.DataFrame(columns=['sector_name', 'change_pct', 'rps_rank'])

    rps_df = pd.DataFrame(results)

    # 按涨幅降序排名，涨幅最高的rank=1
    rps_df['rps_rank'] = rps_df['change_pct'].rank(ascending=False, method='min').astype(int)

    # 按RPS排名排序
    rps_df = rps_df.sort_values('rps_rank').reset_index(drop=True)

    return rps_df


def calculate_rps_history(sector_daily_data: dict, period: int = RPS_PERIOD,
                          history_days: int = None) -> dict:
    """计算多周期RPS排名历史，用于持续性检查

    输入:
        sector_daily_data: {sector_name: DataFrame with 'date' and 'close'}
        period: RPS计算周期
        history_days: 回顾天数（默认=settings.RPS_CONSISTENCY_DAYS）

    返回:
        {sector_name: [rank_day1, rank_day2, ...]}  最近history_days天的排名
    """
    history_days = history_days or RPS_CONSISTENCY_DAYS

    # 获取所有交易日（从所有板块日线中提取）
    all_dates = set()
    for df in sector_daily_data.values():
        all_dates.update(df['date'].tolist())
    all_dates = sorted(all_dates)

    if len(all_dates) < history_days + period:
        return {}

    # 只取最近history_days个交易日，每个交易日都计算RPS排名
    recent_dates = all_dates[-(history_days + period):]

    sector_history = {name: [] for name in sector_daily_data}

    # 对每个需要检查的日期计算RPS
    check_dates = recent_dates[period:]  # 需要period天数据来计算
    for date_str in check_dates:
        fmt = pd.Timestamp(date_str)
        daily_results = []

        for name, df in sector_daily_data.items():
            mask = pd.to_datetime(df['date']) <= fmt
            sub = df[mask]
            if len(sub) >= period + 1:
                recent = sub.tail(period + 1)
                start_p = recent.iloc[0]['close']
                end_p = recent.iloc[-1]['close']
                if not pd.isna(start_p) and not pd.isna(end_p) and start_p > 0:
                    pct = (end_p - start_p) / start_p * 100
                    daily_results.append((name, pct))

        # 排名
        daily_results.sort(key=lambda x: -x[1])
        for rank, (name, _) in enumerate(daily_results, 1):
            if name in sector_history:
                sector_history[name].append(rank)

    return sector_history


def filter_consistent_sectors(sector_history: dict, max_rank: int = None,
                              min_days: int = None) -> list:
    """筛选出RPS排名持续稳定的板块

    条件：板块在过去min_days天中，每天排名都在max_rank以内

    返回: 符合条件的板块名称列表
    """
    max_rank = max_rank or RPS_CONSISTENCY_RANK
    min_days = min_days or RPS_CONSISTENCY_DAYS

    result = []
    for name, ranks in sector_history.items():
        if len(ranks) < min_days:
            continue
        # 检查最近min_days天是否都在max_rank以内
        recent = ranks[-min_days:]
        if all(r <= max_rank for r in recent):
            result.append(name)

    return result


def get_top_sectors(rps_df: pd.DataFrame, top_n: int = RPS_TOP_N) -> list:
    """获取RPS排名前N的板块名称列表"""
    if rps_df.empty:
        return []

    top = rps_df.head(top_n)
    return top['sector_name'].tolist()


def get_dynamic_top_n(market_avg_return: float = None) -> int:
    """根据市场行情动态调整TOP_N

    市场平均涨幅 > threshold: 牛市放宽
    市场平均涨幅 < -threshold: 熊市收紧
    其他: 使用基础值

    参数:
        market_avg_return: 市场平均涨幅（百分比）

    返回: 调整后的TOP_N
    """
    if not RPS_DYNAMIC_ENABLED:
        return RPS_TOP_N

    if market_avg_return is None:
        return RPS_DYNAMIC_BASE_N

    if market_avg_return > RPS_DYNAMIC_THRESHOLD:
        # 牛市：放宽
        extra = int((market_avg_return - RPS_DYNAMIC_THRESHOLD) * 2)
        return min(RPS_DYNAMIC_BASE_N + extra, RPS_DYNAMIC_MAX_N)
    elif market_avg_return < -abs(RPS_DYNAMIC_THRESHOLD):
        # 熊市：收紧
        reduce = int((abs(market_avg_return) - abs(RPS_DYNAMIC_THRESHOLD)) * 2)
        return max(RPS_DYNAMIC_BASE_N - reduce, RPS_DYNAMIC_MIN_N)
    else:
        return RPS_DYNAMIC_BASE_N
