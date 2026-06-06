"""手工选股分析模块

扫描 手工选股 目录，提取图片文件名中的股票代码，
调用通用分析函数进行规则检测，输出每只股票的规则满足情况。

支持格式: png, jpg, jpeg, webp
文件名格式: 股票代码.扩展名 (如 000608.png, 603733.jpg)
"""
import os
import re

import pandas as pd

from config.settings import MANUAL_STOCK_DIR
from src.data_fetcher import DataFetcher
from src.stock_analyzer import batch_analyze_stocks, empty_result
from src.utils import setup_logging

logger = setup_logging("manual_analyzer")

# 支持的图片扩展名
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def scan_manual_stock_codes() -> list:
    """扫描手工选股目录，从图片文件名中提取股票代码

    返回: [code, ...] 的股票代码列表
    """
    if not os.path.isdir(MANUAL_STOCK_DIR):
        logger.warning("手工选股目录不存在: %s", MANUAL_STOCK_DIR)
        return []

    results = []
    for filename in sorted(os.listdir(MANUAL_STOCK_DIR)):
        _, ext = os.path.splitext(filename)
        if ext.lower() not in IMAGE_EXTS:
            continue

        code_match = re.match(r"(\d{6})", filename)
        if code_match:
            code = code_match.group(1)
            results.append(code)
            logger.info("发现手工选股图片: %s → 股票代码 %s", filename, code)
        else:
            logger.debug("跳过无股票代码的文件: %s", filename)

    logger.info("手工选股目录共发现 %d 只股票", len(results))
    return results


def analyze_manual_stocks(
    fetcher: DataFetcher,
    start_date: str,
    end_date: str,
    stock_name_map: dict = None,
) -> pd.DataFrame:
    """分析手工选股目录中的股票是否满足各规则条件

    返回: DataFrame，包含每只股票的规则满足情况
    """
    if stock_name_map is None:
        stock_name_map = {}

    codes = scan_manual_stock_codes()
    if not codes:
        logger.info("手工选股目录无图片，跳过分析")
        return empty_result()

    return batch_analyze_stocks(
        fetcher, codes, start_date, end_date,
        stock_name_map, source_label="手工:", progress_every=10,
    )
