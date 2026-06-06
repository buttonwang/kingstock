"""龙头公司EBK分析模块

解析通达信 .EBK 板块文件，提取其中的股票代码，
调用通用分析函数进行规则检测（宽松版MACD/ZJTJ/KDJ/财务），
输出每只股票的规则满足情况。

EBK文件格式：
  每行7个字符 + CRLF
  第1位: 市场代码 (0=深圳, 1=上海)
  后6位: 股票代码 (如 600519, 000001, 300750)
"""
import os

import pandas as pd

from config.settings import EBK_FILE
from src.data_fetcher import DataFetcher
from src.stock_analyzer import batch_analyze_stocks, empty_result
from src.utils import setup_logging

logger = setup_logging("ebk_analyzer")

# 非A股个股代码前缀（板块指数等）
_EXCLUDED_PREFIXES = ("88", "399", "880", "881")


def parse_ebk_file(filepath: str = None) -> list:
    """解析通达信EBK文件，提取股票代码列表

    返回: [code, ...] 去重后的股票代码列表
    """
    filepath = filepath or EBK_FILE
    if not os.path.isfile(filepath):
        logger.warning("EBK文件不存在: %s", filepath)
        return []

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.error("读取EBK文件失败: %s", e)
        return []

    codes = []
    seen = set()
    for line in lines:
        if len(line) != 7:
            continue

        # 提取后6位作为股票代码
        code = line[1:]
        if not code.isdigit():
            continue

        # 过滤非A股个股代码
        if code.startswith(_EXCLUDED_PREFIXES):
            logger.debug("跳过非个股代码: %s", code)
            continue

        if code not in seen:
            seen.add(code)
            codes.append(code)

    logger.info("EBK文件解析完成: %s，共 %d 只股票（去重后）", filepath, len(codes))
    return codes


def analyze_ebk_stocks(
    fetcher: DataFetcher,
    start_date: str,
    end_date: str,
    stock_name_map: dict = None,
    exclude_codes: set = None,
) -> pd.DataFrame:
    """分析EBK龙头公司列表中的股票是否满足各规则条件

    参数:
        exclude_codes: 需要排除的股票代码集合（如已在主选股结果中的）

    返回: DataFrame，包含每只股票的规则满足情况
    """
    if stock_name_map is None:
        stock_name_map = {}

    all_codes = parse_ebk_file()
    if not all_codes:
        logger.info("EBK文件无有效股票代码，跳过分析")
        return empty_result()

    # 排除已在主选股结果中的股票
    if exclude_codes:
        codes = [c for c in all_codes if c not in exclude_codes]
        logger.info("EBK龙头股排除已有结果 %d 只，剩余 %d 只待分析",
                     len(all_codes) - len(codes), len(codes))
    else:
        codes = all_codes

    if not codes:
        logger.info("EBK龙头股无需分析的股票")
        return empty_result()

    return batch_analyze_stocks(
        fetcher, codes, start_date, end_date,
        stock_name_map, source_label="龙头:", progress_every=50,
    )
