"""规则2：MACD买入信号筛选（V2 增强版）

V2 新增：成交量确认检查，放量买入才视为有效信号
"""
import pandas as pd
from src.indicators.macd import calculate_macd, is_macd_buy_signal
from src.indicators.enhanced_rules import check_volume_confirmation
from config.settings import VOLUME_CONFIRM_ENABLED
from src.utils import setup_logging

logger = setup_logging("macd_filter")


def filter_by_macd(stock_daily_dict: dict) -> set:
    """从股票日线数据中筛选出MACD发出买入信号的股票
    V2 增强：在原有MACD信号基础上，增加成交量确认过滤

    参数: stock_daily_dict - dict, key为股票代码, value为该股票的日线DataFrame
          (需含close列，至少35个交易日的数据)

    流程：
    1. 对每只股票计算MACD指标
    2. 判断是否发出买入信号
    3. 成交量确认（启用时）：确认买入当日放量
    4. 返回符合条件的股票代码集合

    返回: set of stock codes
    """
    total = len(stock_daily_dict)
    logger.info("MACD筛选开始，共 %d 只股票待筛选", total)

    result = set()
    for i, (code, df) in enumerate(stock_daily_dict.items(), 1):
        try:
            if df is None or len(df) < 35:
                continue

            # 计算MACD指标
            df_macd = calculate_macd(df)

            # 判断买入信号
            if not is_macd_buy_signal(df_macd):
                continue

            # V2: 成交量确认（启用时检查）
            if VOLUME_CONFIRM_ENABLED:
                if not check_volume_confirmation(df):
                    continue

            result.add(code)
        except Exception as e:
            logger.warning("股票 %s MACD计算失败: %s", code, e)
            continue

        if i % 500 == 0 or i == total:
            logger.info("MACD筛选进度: %d/%d，当前命中 %d 只", i, total, len(result))

    logger.info("MACD筛选完成: %d 只股票中 %d 只发出买入信号（含成交量确认）", total, len(result))
    return result
