"""自定义选股分析模块（手工选股 + 龙头公司EBK 共用）

提供通用的单只股票规则分析和批量分析函数，
供 manual_analyzer 和 ebk_analyzer 调用。

分析规则（宽松版）：
- MACD: 多头趋势即可（DIF>0, DEA>0, DIF>DEA, MACD>0）
- ZJTJ: 庄家控盘（有庄控盘/高度控盘）
- KDJ: K>D 且 J>0
- 财务: 近三年净利润连续增长 ≥20%

核心条件: MACD ∩ ZJTJ
"""
import pandas as pd

from config.settings import PROFIT_GROWTH_YEARS, PROFIT_GROWTH_MIN
from src.data_fetcher import DataFetcher
from src.indicators.macd import calculate_macd, is_macd_bullish
from src.indicators.kdj import calculate_kdj, is_kdj_bullish
from src.indicators.zjtj import calculate_zjtj, is_zjtj_buy_signal
from src.utils import setup_logging

logger = setup_logging("stock_analyzer")

# 结果DataFrame列名
RESULT_COLUMNS = [
    "code", "name", "source",
    "macd_pass", "zjtj_pass", "kdj_pass", "finance_pass", "core_pass",
    "dif", "dea", "macd", "kongpan", "k", "d", "j",
    "rule_detail",
]


def analyze_stock_rules(
    fetcher: DataFetcher,
    code: str,
    start_date: str,
    end_date: str,
    stock_name_map: dict,
) -> dict:
    """对单只股票进行全套规则分析（宽松版信号检测）

    返回: dict 或 None（数据不足时）
    """
    # 获取日线数据
    df = fetcher.get_stock_daily(code, start_date, end_date)
    if df is None or df.empty or len(df) < 20:
        return None

    name = stock_name_map.get(code, "")

    # ---- MACD（宽松多头趋势） ----
    macd_pass = False
    dif_val, dea_val, macd_val = 0.0, 0.0, 0.0
    try:
        if len(df) >= 35:
            df_macd = calculate_macd(df)
            latest = df_macd.iloc[-1]
            dif_val = round(latest["dif"], 4)
            dea_val = round(latest["dea"], 4)
            macd_val = round(latest["macd"], 4)
            macd_pass = is_macd_bullish(df_macd)
    except Exception:
        pass

    # ---- ZJTJ（庄家控盘） ----
    zjtj_pass = False
    kongpan_val = 0.0
    try:
        df_zjtj = calculate_zjtj(df)
        latest = df_zjtj.iloc[-1]
        kongpan_val = round(latest["kongpan"], 2)
        zjtj_pass = is_zjtj_buy_signal(df_zjtj)
    except Exception:
        pass

    # ---- KDJ（宽松多头趋势） ----
    kdj_pass = False
    k_val, d_val, j_val = 0.0, 0.0, 0.0
    try:
        if len(df) >= 15 and {"high", "low", "close"}.issubset(df.columns):
            df_kdj = calculate_kdj(df)
            latest = df_kdj.iloc[-1]
            k_val = round(latest["k"], 2)
            d_val = round(latest["d"], 2)
            j_val = round(latest["j"], 2)
            kdj_pass = is_kdj_bullish(df_kdj)
    except Exception:
        pass

    # 核心条件（提前判断，不满足则跳过耗时的财务拉取）
    core_pass = macd_pass and zjtj_pass

    # ---- 财务（仅当核心条件满足时才拉取利润数据） ----
    finance_pass = False
    if core_pass:
        try:
            profit_df = fetcher.get_profit_data(code)
            if not profit_df.empty:
                pdf = profit_df.copy()
                pdf["year"] = pd.to_datetime(pdf["report_date"]).dt.year
                pdf = pdf.sort_values("year")
                needed = PROFIT_GROWTH_YEARS + 1
                recent = pdf.tail(needed)
                if len(recent) >= needed:
                    profits = recent["net_profit"].values
                    if all(p > 0 for p in profits):
                        yoy = all(profits[j] > profits[j - 1] for j in range(1, len(profits)))
                        if yoy and profits[0] > 0:
                            cagr = (profits[-1] / profits[0]) ** (1.0 / (len(profits) - 1)) - 1
                            if cagr >= PROFIT_GROWTH_MIN:
                                finance_pass = True
        except Exception:
            pass

    # 规则明细文本
    rule_parts = []
    rule_parts.append("✓MACD买入" if macd_pass else "✗MACD")
    rule_parts.append("✓ZJTJ控盘" if zjtj_pass else "✗ZJTJ")
    if kdj_pass:
        rule_parts.append("✓KDJ买入")
    if finance_pass:
        rule_parts.append("✓财务增长")

    return {
        "code": code,
        "name": name,
        "source": "",
        "macd_pass": macd_pass,
        "zjtj_pass": zjtj_pass,
        "kdj_pass": kdj_pass,
        "finance_pass": finance_pass,
        "core_pass": core_pass,
        "dif": dif_val,
        "dea": dea_val,
        "macd": macd_val,
        "kongpan": kongpan_val,
        "k": k_val,
        "d": d_val,
        "j": j_val,
        "rule_detail": " | ".join(rule_parts),
    }


def batch_analyze_stocks(
    fetcher: DataFetcher,
    codes: list,
    start_date: str,
    end_date: str,
    stock_name_map: dict,
    source_label: str = "",
    progress_every: int = 50,
) -> pd.DataFrame:
    """批量分析一组股票代码

    参数:
        codes: 股票代码列表
        source_label: 来源标签（如 "龙头:" 或 "手工:"）
        progress_every: 每隔多少只打印一次进度

    返回: DataFrame（列为 RESULT_COLUMNS）
    """
    total = len(codes)
    rows = []
    for i, code in enumerate(codes, 1):
        try:
            row = analyze_stock_rules(fetcher, code, start_date, end_date, stock_name_map)
            if row:
                row["source"] = f"{source_label}{code}"
                rows.append(row)
        except Exception as e:
            logger.warning("分析 %s 失败: %s", code, e)

        if i % progress_every == 0 or i == total:
            logger.info("%s分析进度: %d/%d (已完成 %d 只)",
                        source_label, i, total, len(rows))

    if not rows:
        return empty_result()

    df = pd.DataFrame(rows, columns=RESULT_COLUMNS)

    core_count = df["core_pass"].sum()
    macd_count = df["macd_pass"].sum()
    zjtj_count = df["zjtj_pass"].sum()
    kdj_count = df["kdj_pass"].sum()
    fin_count = df["finance_pass"].sum()

    logger.info(
        "%s分析完成: %d只 | 核心(MACD∩ZJTJ)=%d只 | MACD=%d | ZJTJ=%d | KDJ=%d | 财务=%d",
        source_label, len(df), core_count, macd_count, zjtj_count, kdj_count, fin_count,
    )

    return df


def empty_result() -> pd.DataFrame:
    """返回空结果DataFrame"""
    return pd.DataFrame(columns=RESULT_COLUMNS)
