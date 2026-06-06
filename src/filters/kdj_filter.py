"""规则4：KDJ买入信号筛选"""
import pandas as pd
from src.indicators.kdj import calculate_kdj, is_kdj_buy_signal
from src.utils import setup_logging

logger = setup_logging("kdj_filter")


def filter_by_kdj(stock_daily_dict: dict) -> set:
    """从股票日线数据中筛选出KDJ发出买入信号的股票

    参数: stock_daily_dict - dict, key为股票代码, value为该股票的日线DataFrame
          (需含high,low,close列，至少15个交易日数据)

    流程：
    1. 对每只股票计算KDJ指标
    2. 判断是否发出买入信号
    3. 返回符合条件的股票代码集合

    返回: set of stock codes
    """
    total = len(stock_daily_dict)
    logger.info("KDJ筛选开始，共 %d 只股票待筛选", total)

    result = set()
    for i, (code, df) in enumerate(stock_daily_dict.items(), 1):
        try:
            if df is None or len(df) < 15:
                continue

            # 检查必要列
            required_cols = {'high', 'low', 'close'}
            if not required_cols.issubset(df.columns):
                continue

            # 计算KDJ指标
            df_kdj = calculate_kdj(df)

            # 判断买入信号
            if is_kdj_buy_signal(df_kdj):
                result.add(code)
        except Exception as e:
            logger.warning("股票 %s KDJ计算失败: %s", code, e)
            continue

        if i % 500 == 0 or i == total:
            logger.info("KDJ筛选进度: %d/%d，当前命中 %d 只", i, total, len(result))

    logger.info("KDJ筛选完成: %d 只股票中 %d 只发出买入信号", total, len(result))
    return result
