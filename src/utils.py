"""通用工具函数"""
import logging
import os
from datetime import datetime, timedelta
import pandas as pd
from config.settings import LOG_PATH


def setup_logging(name: str = "stock_selector") -> logging.Logger:
    """配置日志（避免重复handler）"""
    os.makedirs(LOG_PATH, exist_ok=True)
    logger = logging.getLogger(name)
    
    # 已配置过则直接返回，避免重复handler
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # 控制台handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    
    # 文件handler
    fh = logging.FileHandler(
        os.path.join(LOG_PATH, f"{name}.log"), encoding='utf-8'
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    
    return logger


def get_trade_date(date_str: str = None) -> str:
    """获取交易日期，如果未指定则返回当天日期(YYYYMMDD格式)

    使用AKShare交易日历判断真实的A股交易日，
    支持春节、国庆等法定节假日判断。
    """
    if date_str:
        try:
            datetime.strptime(date_str, "%Y%m%d")
            return date_str
        except ValueError:
            raise ValueError(f"日期格式错误: {date_str}，请使用YYYYMMDD格式")

    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")

    # 尝试使用AKShare交易日历判断
    try:
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        cal['trade_date'] = pd.to_datetime(cal['trade_date']).dt.strftime("%Y-%m-%d")
        
        # 过滤当年的交易日
        year_str = str(today.year)
        year_cal = cal[cal['trade_date'].str.startswith(year_str)]
        
        if today_str in year_cal['trade_date'].values:
            return today_str.replace('-', '')
        
        # 不是交易日，找最近的前一个交易日
        past_dates = year_cal[year_cal['trade_date'] <= today_str]['trade_date'].values
        if len(past_dates) > 0:
            return past_dates[-1].replace('-', '')
    except Exception:
        pass

    # 兜底：仅处理周末（AKShare不可用时的fallback）
    if today.weekday() == 5:  # Saturday
        today -= timedelta(days=1)
    elif today.weekday() == 6:  # Sunday
        today -= timedelta(days=2)
    return today.strftime("%Y%m%d")


def format_stock_table(df: pd.DataFrame) -> str:
    """将选股结果DataFrame格式化为对齐的文本表格

    期望列: code, name, sector, dif, dea, macd, k, d, j, kongpan
    """
    if df is None or df.empty:
        return ""

    # 定义每列的显示宽度和格式
    col_config = {
        "code":    {"width": 8,  "header": "代码",   "fmt": "{}"},
        "name":    {"width": 8,  "header": "名称",   "fmt": "{}"},
        "sector":  {"width": 10, "header": "板块",   "fmt": "{}"},
        "dif":     {"width": 10, "header": "DIF",    "fmt": "{:.4f}"},
        "dea":     {"width": 10, "header": "DEA",    "fmt": "{:.4f}"},
        "macd":    {"width": 10, "header": "MACD",   "fmt": "{:.4f}"},
        "k":       {"width": 8,  "header": "K",      "fmt": "{:.2f}"},
        "d":       {"width": 8,  "header": "D",      "fmt": "{:.2f}"},
        "j":       {"width": 8,  "header": "J",      "fmt": "{:.2f}"},
        "kongpan": {"width": 10, "header": "控盘度",  "fmt": "{:.2f}"},
    }

    # 只处理DataFrame中实际存在的列
    cols = [c for c in col_config if c in df.columns]

    # 中文字符宽度处理：计算实际显示宽度
    def display_width(s: str) -> int:
        """计算字符串的显示宽度（中文字符占2个宽度）"""
        width = 0
        for ch in str(s):
            if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f':
                width += 2
            else:
                width += 1
        return width

    def pad_str(s: str, target_width: int, align: str = "left") -> str:
        """按显示宽度填充字符串到目标宽度"""
        current = display_width(s)
        padding = max(0, target_width - current)
        if align == "left":
            return s + " " * padding
        else:
            return " " * padding + s

    lines = []

    # 表头
    headers = []
    for col in cols:
        cfg = col_config[col]
        headers.append(pad_str(cfg["header"], cfg["width"]))
    lines.append("  ".join(headers))

    # 分隔线
    separators = []
    for col in cols:
        cfg = col_config[col]
        separators.append("-" * cfg["width"])
    lines.append("  ".join(separators))

    # 数据行
    for _, row in df.iterrows():
        cells = []
        for col in cols:
            cfg = col_config[col]
            val = row[col]
            if pd.isna(val):
                text = ""
            elif col in ("code", "name", "sector"):
                text = str(val)
            else:
                text = cfg["fmt"].format(val)
            # 数值列右对齐，文本列左对齐
            align = "right" if col not in ("code", "name", "sector") else "left"
            cells.append(pad_str(text, cfg["width"], align))
        lines.append("  ".join(cells))

    return "\n".join(lines)