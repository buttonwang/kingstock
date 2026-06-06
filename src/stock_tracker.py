"""选股结果历史追踪 - 记录选股结果至数据库，计算连续出现天数和涨跌幅"""

import sqlite3
import os
from datetime import datetime, timedelta

import pandas as pd

from config.settings import DB_PATH
from src.utils import setup_logging

logger = setup_logging("stock_tracker")

_CREATE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS stock_history (
    date       TEXT,    -- YYYY-MM-DD
    code       TEXT,    -- 股票代码
    source     TEXT,    -- 'main'主选 | 'manual'手工 | 'ebk'龙头
    PRIMARY KEY (date, code, source)
);
"""

_CREATE_HTML_TABLE = """
CREATE TABLE IF NOT EXISTS html_reports (
    date       TEXT PRIMARY KEY,   -- YYYY-MM-DD
    content    TEXT,               -- HTML内容
    created_at TEXT                -- 创建时间
);
"""


class StockTracker:
    """选股结果历史追踪器

    功能:
        1. 将每日选股结果记录到 stock_history 表
        2. 查询某股票连续出现天数
        3. 结合日线数据计算涨跌幅（今日、3日、5日、10日）
    """

    def __init__(self):
        """初始化，确保 history 表存在"""
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.execute(_CREATE_HISTORY_TABLE)
        self._conn.execute(_CREATE_HTML_TABLE)
        self._conn.commit()
        logger.info("StockTracker 初始化完成，数据库: %s", DB_PATH)

    # ------------------------------------------------------------------
    # HTML报告存储
    # ------------------------------------------------------------------

    def save_html_report(self, date_str: str, html_content: str):
        """将HTML报告内容保存到数据库

        参数:
            date_str: 日期 YYYYMMDD
            html_content: HTML字符串
        """
        fmt_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO html_reports (date, content, created_at) VALUES (?, ?, ?)",
                (fmt_date, html_content, now_str),
            )
            self._conn.commit()
            logger.info("HTML报告已保存到数据库: %s", fmt_date)
        except Exception as e:
            logger.warning("保存HTML报告到数据库失败: %s", e)

    def get_html_report(self, date_str: str) -> str:
        """从数据库获取指定日期的HTML报告

        参数:
            date_str: 日期 YYYYMMDD

        返回:
            HTML字符串，不存在返回空字符串
        """
        fmt_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        try:
            row = self._conn.execute(
                "SELECT content FROM html_reports WHERE date = ?",
                (fmt_date,),
            ).fetchone()
            return row[0] if row else ""
        except Exception as e:
            logger.warning("从数据库获取HTML报告失败: %s", e)
            return ""

    def close(self):
        if self._conn:
            self._conn.close()
            logger.info("StockTracker 数据库连接已关闭")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # 记录结果
    # ------------------------------------------------------------------

    def record_results(self, result_df: pd.DataFrame = None,
                       manual_df: pd.DataFrame = None,
                       ebk_df: pd.DataFrame = None,
                       date_str: str = None) -> int:
        """将今日选股结果中满足核心条件的股票记录到历史表

        参数:
            result_df: 主选股结果 DataFrame
            manual_df: 手工选股结果 DataFrame
            ebk_df: 龙头EBK分析结果 DataFrame
            date_str: 日期 YYYYMMDD

        返回:
            写入的记录数
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        # 格式化为 YYYY-MM-DD
        fmt_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        rows = []

        # 主选股（所有结果都记录）
        if result_df is not None and not result_df.empty and "code" in result_df.columns:
            for code in result_df["code"].tolist():
                rows.append((fmt_date, str(code).strip(), "main"))

        # 手工选股（仅 core_pass）
        if manual_df is not None and not manual_df.empty:
            core = manual_df[manual_df["core_pass"]] if "core_pass" in manual_df.columns else manual_df
            if "code" in core.columns:
                for code in core["code"].tolist():
                    rows.append((fmt_date, str(code).strip(), "manual"))

        # 龙头EBK（仅 core_pass）
        if ebk_df is not None and not ebk_df.empty:
            core = ebk_df[ebk_df["core_pass"]] if "core_pass" in ebk_df.columns else ebk_df
            if "code" in core.columns:
                for code in core["code"].tolist():
                    rows.append((fmt_date, str(code).strip(), "ebk"))

        if not rows:
            logger.info("今日无记录写入（无选股结果）")
            return 0

        # 去重写入
        cur = self._conn.cursor()
        inserted = 0
        for row in rows:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO stock_history (date, code, source) VALUES (?, ?, ?)",
                    row,
                )
                if cur.rowcount > 0:
                    inserted += 1
            except Exception as e:
                logger.warning("写入历史记录失败: %s - %s", row, e)
        self._conn.commit()
        cur.close()

        logger.info("已记录 %d 条选股结果至历史表（共 %d 条）", inserted, len(rows))
        return inserted

    # ------------------------------------------------------------------
    # 查询连续出现天数
    # ------------------------------------------------------------------

    def get_consecutive_days(self, code: str, date_str: str) -> int:
        """查询某股票从 date_str 起连续出现在选股结果中的天数

        算法:
            - 从 date_str 当天往前检查
            - 只要当天在 history 表中存在，天数+1
            - 遇到缺失的日期则中断
            - 对于非交易日（如周末），跳过（不中断连续性）

        参数:
            code: 股票代码
            date_str: 查询日期起始点 YYYYMMDD

        返回:
            连续出现天数（至少返回1，表示当天也有记录）
        """
        fmt_end = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

        # 查今天是否有记录
        today_count = self._conn.execute(
            "SELECT COUNT(*) FROM stock_history WHERE code = ? AND date = ?",
            (code, fmt_end),
        ).fetchone()[0]

        if today_count == 0:
            return 0

        # 获取该股票所有历史日期（降序）
        rows = self._conn.execute(
            "SELECT DISTINCT date FROM stock_history WHERE code = ? ORDER BY date DESC",
            (code,),
        ).fetchall()

        dates = [r[0] for r in rows]

        if fmt_end not in dates:
            return 0

        # 从今天开始往前数连续的天数
        idx = dates.index(fmt_end)
        consecutive = 1

        current_date = pd.Timestamp(fmt_end)
        for i in range(idx + 1, len(dates)):
            prev_date = pd.Timestamp(dates[i])
            # 计算实际间隔天数
            delta = (current_date - prev_date).days
            if delta == 1:
                consecutive += 1
                current_date = prev_date
            elif delta == 0:
                continue  # 同一天多个来源，跳过
            else:
                # 间隔超过1天（非交易日），中断连续性
                break

        return consecutive

    # ------------------------------------------------------------------
    # 涨跌幅计算
    # ------------------------------------------------------------------

    @staticmethod
    def get_recent_returns(fetcher, code: str, end_date_str: str) -> dict:
        """计算某股票今日涨跌幅及近3/5/10日涨幅

        参数:
            fetcher: DataFetcher 实例（用于获取日线数据）
            code: 股票代码
            end_date_str: 截止日期 YYYYMMDD

        返回:
            dict: {change_pct, return_3d, return_5d, return_10d, consecutive_days}
                 值为 float 或 None
        """
        # 计算起始日期：往前取15个交易日保险（至少需要11个交易日）
        end_dt = datetime.strptime(end_date_str, "%Y%m%d")
        start_dt = end_dt - timedelta(days=25)  # 取25天前的日期，确保有足够交易日
        start_str = start_dt.strftime("%Y%m%d")

        try:
            df = fetcher.get_stock_daily(code, start_str, end_date_str)
        except Exception as e:
            logger.warning("获取 %s 日线失败: %s", code, e)
            return {
                "change_pct": None,
                "return_3d": None,
                "return_5d": None,
                "return_10d": None,
            }

        if df is None or df.empty:
            return {
                "change_pct": None,
                "return_3d": None,
                "return_5d": None,
                "return_10d": None,
            }

        # 确保有序且列名正确
        df = df.sort_values("date").reset_index(drop=True)
        if "close" not in df.columns:
            return {
                "change_pct": None,
                "return_3d": None,
                "return_5d": None,
                "return_10d": None,
            }

        closes = df["close"].values
        n = len(closes)

        def _pct(new_idx, old_idx):
            """计算涨幅百分比"""
            # 转换负索引为绝对索引
            abs_new = new_idx if new_idx >= 0 else n + new_idx
            abs_old = old_idx if old_idx >= 0 else n + old_idx
            if abs_new < 0 or abs_old < 0 or abs_new >= n or abs_old >= n:
                return None
            if closes[old_idx] == 0:
                return None
            return round((closes[new_idx] / closes[old_idx] - 1) * 100, 2)

        return {
            "change_pct": _pct(-1, -2),    # 今日 vs 昨日
            "return_3d": _pct(-1, -4),     # 今日 vs 3日前
            "return_5d": _pct(-1, -6),     # 今日 vs 5日前
            "return_10d": _pct(-1, -11),   # 今日 vs 10日前
        }

    # ------------------------------------------------------------------
    # 批量计算
    # ------------------------------------------------------------------

    def compute_all_returns(self, fetcher, result_df: pd.DataFrame = None,
                            manual_df: pd.DataFrame = None,
                            ebk_df: pd.DataFrame = None,
                            date_str: str = None) -> dict:
        """批量计算所有选股结果中股票的涨跌幅和连续天数

        返回:
            {code: {change_pct, return_3d, return_5d, return_10d, consecutive_days}}
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y%m%d")

        all_codes = set()

        if result_df is not None and not result_df.empty and "code" in result_df.columns:
            all_codes.update(result_df["code"].tolist())

        if manual_df is not None and not manual_df.empty and "code" in manual_df.columns:
            all_codes.update(manual_df["code"].tolist())

        if ebk_df is not None and not ebk_df.empty and "code" in ebk_df.columns:
            all_codes.update(ebk_df["code"].tolist())

        result = {}
        for code in all_codes:
            code_str = str(code).strip()
            ret = self.get_recent_returns(fetcher, code_str, date_str)
            ret["consecutive_days"] = self.get_consecutive_days(code_str, date_str)
            result[code_str] = ret

        logger.info("计算了 %d 只股票的涨跌幅和连续天数", len(result))
        return result
