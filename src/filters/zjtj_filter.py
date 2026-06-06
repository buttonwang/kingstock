"""规则3：庄家控盘信号筛选"""
import pandas as pd
from src.indicators.zjtj import calculate_zjtj, is_zjtj_buy_signal
from src.utils import setup_logging

logger = setup_logging("zjtj_filter")


def filter_by_zjtj(stock_daily_dict: dict) -> set:
    """从股票日线数据中筛选出有庄控盘信号的股票

    参数: stock_daily_dict - dict, key为股票代码, value为该股票的日线DataFrame
          (需含close列，至少20个交易日的数据)

    流程：
    1. 对每只股票计算ZJTJ指标
    2. 判断是否为有庄控盘
    3. 返回符合条件的股票代码集合

    返回: set of stock codes
    """
    total = len(stock_daily_dict)
    logger.info("ZJTJ筛选开始，共 %d 只股票待筛选", total)

    result = set()
    for i, (code, df) in enumerate(stock_daily_dict.items(), 1):
        try:
            if df is None or len(df) < 20:
                continue

            # 计算ZJTJ指标
            df_zjtj = calculate_zjtj(df)

            # 判断有庄控盘信号
            if is_zjtj_buy_signal(df_zjtj):
                result.add(code)
        except Exception as e:
            logger.warning("股票 %s ZJTJ计算失败: %s", code, e)
            continue

        if i % 500 == 0 or i == total:
            logger.info("ZJTJ筛选进度: %d/%d，当前命中 %d 只", i, total, len(result))

    logger.info("ZJTJ筛选完成: %d 只股票中 %d 只发出控盘信号", total, len(result))
    return result
