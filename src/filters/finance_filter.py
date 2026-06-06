"""规则5：财务基本面筛选 - 近三年净利润连续增长超20%（已弃用，由StockSelector._filter_finance_for_candidates替代）"""
import pandas as pd
from src.data_fetcher import DataFetcher
from config.settings import PROFIT_GROWTH_YEARS, PROFIT_GROWTH_MIN
from src.utils import setup_logging

logger = setup_logging("finance_filter")


def filter_by_finance(fetcher: DataFetcher) -> set:
    """筛选近三年净利润连续增长且增幅超过20%的股票

    注意: 此函数遍历所有A股（5000+只），效率较低。
    实际选股引擎中已改用 StockSelector._filter_finance_for_candidates()，
    只扫描候选股票，效率更高。此函数保留仅为接口兼容。
    """
    logger.warning("filter_by_finance 已废弃，请改用 StockSelector._filter_finance_for_candidates")
    return _filter_finance(fetcher)


def _filter_finance(fetcher: DataFetcher) -> set:
    """财务筛选核心逻辑（供内部使用）"""
    profit_df = fetcher.get_all_profit_data()
    if profit_df.empty:
        logger.warning("未获取到净利润数据")
        return set()

    logger.info("共获取 %d 条净利润记录，开始筛选", len(profit_df))

    result = set()
    grouped = profit_df.groupby('code')

    total = len(grouped)
    for i, (code, group) in enumerate(grouped, 1):
        try:
            group = group.sort_values('year')
            needed_years = PROFIT_GROWTH_YEARS + 1
            recent = group.tail(needed_years)

            if len(recent) < needed_years:
                continue

            profits = recent['net_profit'].values

            if any(p <= 0 for p in profits):
                continue

            year_over_year_growth = True
            for j in range(1, len(profits)):
                if profits[j] <= profits[j - 1]:
                    year_over_year_growth = False
                    break

            if not year_over_year_growth:
                continue

            earliest_profit = profits[0]
            latest_profit = profits[-1]
            n_years = len(profits) - 1

            if earliest_profit <= 0:
                continue

            cagr = (latest_profit / earliest_profit) ** (1.0 / n_years) - 1

            if cagr >= PROFIT_GROWTH_MIN:
                result.add(code)

        except Exception as e:
            logger.warning("股票 %s 财务筛选失败: %s", code, e)
            continue

        if i % 500 == 0 or i == total:
            logger.info("财务筛选进度: %d/%d，当前命中 %d 只", i, total, len(result))

    logger.info("财务筛选完成: %d 只股票中 %d 只满足增长条件", total, len(result))
    return result
