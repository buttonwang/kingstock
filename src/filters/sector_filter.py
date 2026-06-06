"""规则1：热门板块RPS筛选 - 股票需在RPS前N板块中（V2 增强版）

V2 新增：
- 双阶段RPS：行业板块初筛 + 概念板块精选
- RPS持续性检查：板块需连续N日在排名前30
- 动态TOP_N：根据市场行情调整
"""
import pandas as pd
from src.data_fetcher import DataFetcher
from src.indicators.rps import (
    calculate_sector_rps, get_top_sectors, get_dynamic_top_n,
    calculate_rps_history, filter_consistent_sectors,
)
from config.settings import (
    RPS_PERIOD, RPS_TOP_N, SECTOR_TYPE,
    DUAL_RPS_ENABLED, RPS_PRIMARY_TOP_N, RPS_SECONDARY_TOP_N,
    RPS_CONSISTENCY_ENABLED,
    RPS_DYNAMIC_ENABLED,
)
from src.utils import setup_logging

logger = setup_logging("sector_filter")


def _get_sector_stock_codes(fetcher: DataFetcher, sector_names: list,
                             sector_type: str) -> set:
    """获取一组板块的所有成分股"""
    stock_codes = set()
    for sector_name in sector_names:
        try:
            constituents = fetcher.get_sector_constituents(sector_name, sector_type)
            if constituents is not None and not constituents.empty:
                codes = set(constituents['code'].tolist())
                stock_codes.update(codes)
        except Exception as e:
            logger.warning("获取板块 '%s' 成分股失败: %s", sector_name, e)
            continue
    return stock_codes


def _load_sector_data(fetcher: DataFetcher, sector_type: str,
                       start_date: str, end_date: str) -> dict:
    """加载指定类型的所有板块行情数据"""
    sectors_df = fetcher.get_sector_list(sector_type)
    if sectors_df.empty:
        logger.warning("未获取到板块列表 (type=%s)", sector_type)
        return {}

    sector_names = sectors_df['sector_name'].tolist()
    sector_daily_data = {}

    for i, name in enumerate(sector_names, 1):
        try:
            df = fetcher.get_sector_daily(name, sector_type, start_date, end_date)
            if df is not None and not df.empty:
                sector_daily_data[name] = df
        except Exception as e:
            logger.warning("获取板块 '%s' 行情失败: %s", name, e)
            continue

        if i % 50 == 0 or i == len(sector_names):
            logger.info("板块行情获取进度(%s): %d/%d", sector_type, i, len(sector_names))

    logger.info("成功获取 %d 个(%s)板块的行情数据", len(sector_daily_data), sector_type)
    return sector_daily_data


def filter_by_sector_rps(fetcher: DataFetcher, start_date: str, end_date: str,
                         sector_type: str = None) -> set:
    """筛选出位于RPS排名前N板块中的股票代码集合

    参数:
        sector_type: "concept"(概念板块) 或 "industry"(行业板块)，
                     默认使用 settings.SECTOR_TYPE

    当 DUAL_RPS_ENABLED=True 时，自动启用双阶段RPS：
    1. 行业板块RPS初筛（取前RPS_PRIMARY_TOP_N）
    2. 概念板块RPS精选（取前RPS_SECONDARY_TOP_N）
    3. 取交集以减少噪音

    返回: set of stock codes
    """
    sector_type = sector_type or SECTOR_TYPE

    # 双阶段RPS
    if DUAL_RPS_ENABLED:
        return _filter_by_dual_rps(fetcher, start_date, end_date)

    # 单阶段RPS
    sector_daily_data = _load_sector_data(fetcher, sector_type, start_date, end_date)
    if not sector_daily_data:
        return set()

    top_n = get_dynamic_top_n() if RPS_DYNAMIC_ENABLED else RPS_TOP_N

    if RPS_CONSISTENCY_ENABLED:
        return _filter_with_consistency(fetcher, sector_daily_data, sector_type, top_n)

    # 标准流程
    rps_df = calculate_sector_rps(sector_daily_data, period=RPS_PERIOD)
    if rps_df.empty:
        logger.warning("RPS计算结果为空")
        return set()

    top_sectors = get_top_sectors(rps_df, top_n=top_n)
    logger.info("RPS排名前 %d 板块 (%s): %s", top_n, sector_type, top_sectors[:10])

    stock_codes = _get_sector_stock_codes(fetcher, top_sectors, sector_type)
    logger.info("板块RPS筛选完成(%s): 前%d板块共包含 %d 只股票",
                sector_type, top_n, len(stock_codes))
    return stock_codes


def _filter_with_consistency(fetcher: DataFetcher, sector_daily_data: dict,
                              sector_type: str, top_n: int) -> set:
    """带持续性检查的RPS筛选"""
    # 先计算当日RPS
    rps_df = calculate_sector_rps(sector_daily_data, period=RPS_PERIOD)
    if rps_df.empty:
        return set()

    # 计算排名历史
    history = calculate_rps_history(sector_daily_data)
    if history:
        consistent = filter_consistent_sectors(history)
        # 取当日TOP_N 与 持续达标的交集
        top_today = set(get_top_sectors(rps_df, top_n=top_n))
        top_sectors = list(top_today & set(consistent))
        if not top_sectors:
            # 没有交集时回退到当日TOP_N
            top_sectors = get_top_sectors(rps_df, top_n=top_n)
    else:
        top_sectors = get_top_sectors(rps_df, top_n=top_n)

    logger.info("RPS排名(一致性)前 %d 板块 (%s): %s", top_n, sector_type, top_sectors[:10])
    stock_codes = _get_sector_stock_codes(fetcher, top_sectors, sector_type)
    return stock_codes


def _filter_by_dual_rps(fetcher: DataFetcher, start_date: str,
                         end_date: str) -> set:
    """双阶段RPS筛选

    阶段1：行业板块RPS初筛（取前RPS_PRIMARY_TOP_N）
    阶段2：概念板块RPS精选（取前RPS_SECONDARY_TOP_N）
    返回：同时属于两阶段板块的股票（交集）
    """
    logger.info("=" * 60)
    logger.info("启用双阶段RPS筛选")
    logger.info("=" * 60)

    # 阶段1：行业板块（初筛）
    ind_data = _load_sector_data(fetcher, "industry", start_date, end_date)
    if not ind_data:
        logger.warning("行业板块数据为空，回退到概念板块")
        return _filter_single_rps_fallback(fetcher, start_date, end_date)

    rps_ind = calculate_sector_rps(ind_data, period=RPS_PERIOD)
    if rps_ind.empty:
        return _filter_single_rps_fallback(fetcher, start_date, end_date)

    top_ind = get_top_sectors(rps_ind, top_n=RPS_PRIMARY_TOP_N)
    logger.info("阶段1(行业板块) 前%d: %s", RPS_PRIMARY_TOP_N, top_ind[:10])
    pool_a = _get_sector_stock_codes(fetcher, top_ind, "industry")
    logger.info("阶段1 候选池: %d 只股票", len(pool_a))

    # 阶段2：概念板块（精选）
    con_data = _load_sector_data(fetcher, "concept", start_date, end_date)
    if not con_data:
        logger.warning("概念板块数据为空，使用阶段1结果")
        return pool_a

    rps_con = calculate_sector_rps(con_data, period=RPS_PERIOD)
    if rps_con.empty:
        return pool_a

    top_con = get_top_sectors(rps_con, top_n=RPS_SECONDARY_TOP_N)
    logger.info("阶段2(概念板块) 前%d: %s", RPS_SECONDARY_TOP_N, top_con[:10])
    pool_b = _get_sector_stock_codes(fetcher, top_con, "concept")
    logger.info("阶段2 候选池: %d 只股票", len(pool_b))

    # 取交集
    final_pool = pool_a & pool_b
    logger.info("双阶段RPS完成: 阶段1=%d只 ∩ 阶段2=%d只 = %d只",
                len(pool_a), len(pool_b), len(final_pool))
    logger.info("=" * 60)
    return final_pool


def _filter_single_rps_fallback(fetcher: DataFetcher, start_date: str,
                                 end_date: str) -> set:
    """双阶段RPS失败时的回退：使用默认SECTOR_TYPE"""
    logger.warning("双阶段RPS数据不足，回退到单阶段RPS")
    sector_type = SECTOR_TYPE
    sector_data = _load_sector_data(fetcher, sector_type, start_date, end_date)
    if not sector_data:
        return set()
    rps_df = calculate_sector_rps(sector_data, period=RPS_PERIOD)
    if rps_df.empty:
        return set()
    top_sectors = get_top_sectors(rps_df, top_n=RPS_TOP_N)
    return _get_sector_stock_codes(fetcher, top_sectors, sector_type)
