"""AKShare数据获取模块，带SQLite缓存 (使用同花顺/新浪/腾讯数据源)"""
import time
import sqlite3
from datetime import datetime
from contextlib import contextmanager

import pandas as pd
import akshare as ak
import requests
from bs4 import BeautifulSoup
import re

from config.settings import DB_PATH, REQUEST_RETRY, REQUEST_DELAY, REQUEST_TIMEOUT
from src.utils import setup_logging

logger = setup_logging("data_fetcher")

# ---------------------------------------------------------------------------
# 列名映射
# ---------------------------------------------------------------------------

# 新浪源 stock_zh_a_daily 列名 → 系统统一列名
_DAILY_COL_MAP = {
    "date": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "amount": "amount",
    "turnover": "turnover_rate",
}

# 同花顺板块日线列名映射
_SECTOR_DAILY_COL_MAP = {
    "日期": "date",
    "开盘价": "open",
    "收盘价": "close",
    "最高价": "high",
    "最低价": "low",
    "成交量": "volume",
    "成交额": "amount",
}

# 财务数据列名映射
_PROFIT_COL_MAP = {
    "股票代码": "code",
    "报告期": "report_date",
    "净利润": "net_profit",
}

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def code_to_ths_prefix(symbol: str) -> str:
    """纯数字股票代码转为新浪/腾讯/同花顺使用的带前缀格式

    规则：
        6 开头 → sh（沪市）
        0/3 开头 → sz（深市）
    """
    symbol = symbol.strip().upper()
    if symbol.startswith(("SH", "SZ")):
        return symbol
    if symbol.startswith("6"):
        return "SH" + symbol
    return "SZ" + symbol


# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS stock_list (
    code   TEXT PRIMARY KEY,
    name   TEXT
);

CREATE TABLE IF NOT EXISTS stock_daily (
    code           TEXT,
    date           TEXT,
    open           REAL,
    high           REAL,
    low            REAL,
    close          REAL,
    volume         REAL,
    turnover_rate  REAL,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS sector_list (
    sector_name  TEXT,
    sector_code  TEXT,
    sector_type  TEXT,
    PRIMARY KEY (sector_name, sector_type)
);

CREATE TABLE IF NOT EXISTS sector_constituents (
    sector_name  TEXT,
    sector_type  TEXT,
    code         TEXT,
    name         TEXT,
    PRIMARY KEY (sector_name, sector_type, code)
);

CREATE TABLE IF NOT EXISTS sector_daily (
    sector_name  TEXT,
    sector_type  TEXT,
    date         TEXT,
    close        REAL,
    change_pct   REAL,
    PRIMARY KEY (sector_name, sector_type, date)
);

CREATE TABLE IF NOT EXISTS profit_data (
    code       TEXT,
    year       INTEGER,
    net_profit REAL,
    PRIMARY KEY (code, year)
);
"""


# ---------------------------------------------------------------------------
# DataFetcher
# ---------------------------------------------------------------------------


class DataFetcher:
    """AKShare数据获取器，带SQLite缓存（使用同花顺/新浪/腾讯数据源）"""

    def __init__(self):
        """初始化数据库连接，确保目录和表存在"""
        import os
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._conn.executescript(_CREATE_TABLES)
        self._conn.commit()
        logger.info("DataFetcher 初始化完成，数据库: %s", DB_PATH)

    # ---- 上下文管理器 -------------------------------------------------------

    def close(self):
        if self._conn:
            self._conn.close()
            logger.info("数据库连接已关闭")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 内部工具 ------------------------------------------------------------

    @contextmanager
    def _cursor(self):
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    @staticmethod
    def _retry(func, *args, **kwargs):
        """带重试和延迟的请求包装"""
        last_err = None
        for attempt in range(1, REQUEST_RETRY + 1):
            try:
                result = func(*args, **kwargs)
                time.sleep(REQUEST_DELAY)
                return result
            except Exception as e:
                last_err = e
                logger.warning("请求失败 (第%d次/%d): %s", attempt, REQUEST_RETRY, e)
                if attempt < REQUEST_RETRY:
                    time.sleep(REQUEST_DELAY * attempt)
        raise last_err

    def _df_to_sql(self, df: pd.DataFrame, table: str, if_exists: str = "replace"):
        """将 DataFrame 写入 SQLite 表"""
        df.to_sql(table, self._conn, if_exists=if_exists, index=False)

    def _sql_to_df(self, sql: str, params=None) -> pd.DataFrame:
        """从 SQLite 读取 DataFrame"""
        return pd.read_sql_query(sql, self._conn, params=params)

    # ---- 公开 API ------------------------------------------------------------

    def get_all_stocks(self) -> pd.DataFrame:
        """获取所有A股股票代码和名称列表

        返回: DataFrame[code, name]
        """
        cached = self._sql_to_df("SELECT code, name FROM stock_list")
        if not cached.empty:
            logger.info("从缓存加载股票列表，共 %d 只", len(cached))
            return cached

        logger.info("正在从 AKShare 获取全部A股列表...")
        raw = self._retry(ak.stock_info_a_code_name)
        df = raw[["code", "name"]]
        self._df_to_sql(df, "stock_list", if_exists="replace")
        logger.info("已缓存 %d 只股票", len(df))
        return df

    # -----------------------------------------------------------------------

    def get_stock_daily(self, symbol: str, start_date: str, end_date: str,
                        adjust: str = "qfq") -> pd.DataFrame:
        """获取单只股票日线数据（使用新浪源 + SQLite缓存）

        缓存策略：先查缓存，缺失部分增量拉取，避免重复请求。
        返回: DataFrame[date, open, high, low, close, volume, turnover_rate]
        """
        s_start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        s_end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

        # 1. 查缓存
        cached = self._sql_to_df(
            "SELECT date, open, high, low, close, volume, turnover_rate FROM stock_daily "
            "WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            params=(symbol, s_start, s_end),
        )

        if not cached.empty:
            latest_cached = cached['date'].max()
            if latest_cached >= s_end:
                logger.info("从缓存加载 %s 日线数据，共 %d 条", symbol, len(cached))
                return cached
            # 只需拉取缓存之后的数据
            fetch_start = (pd.Timestamp(latest_cached) + pd.Timedelta(days=1)).strftime("%Y%m%d")
        else:
            fetch_start = start_date

        # 2. 从API拉取缺失数据
        prefixed = code_to_ths_prefix(symbol)
        logger.info("获取 %s(%s) 日线（增量 %s ~ %s）", symbol, prefixed, fetch_start, end_date)

        raw = self._retry(
            ak.stock_zh_a_daily,
            symbol=prefixed.lower(),
            start_date=fetch_start,
            end_date=end_date,
            adjust=adjust,
        )

        if raw.empty:
            if not cached.empty:
                return cached
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        # 3. 处理新数据
        df = raw.rename(columns=_DAILY_COL_MAP)
        keep = ["date", "open", "high", "low", "close", "amount", "turnover_rate"]
        for col in keep:
            if col not in df.columns:
                df[col] = None
        df = df[keep]
        df["volume"] = 0.0
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        for col in ["open", "high", "low", "close", "amount", "turnover_rate", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

        new_result = df[["date", "open", "high", "low", "close", "volume", "turnover_rate"]]

        # 4. 回写入缓存
        cache_df = new_result.copy()
        cache_df["code"] = symbol
        with self._cursor() as cur:
            for _, row in cache_df.iterrows():
                cur.execute(
                    "INSERT OR REPLACE INTO stock_daily "
                    "(code, date, open, high, low, close, volume, turnover_rate) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["code"], row["date"], row["open"], row["high"],
                     row["low"], row["close"], row["volume"], row["turnover_rate"]),
                )

        # 5. 合并返回
        if not cached.empty:
            combined = pd.concat([cached, new_result])
            combined = combined.drop_duplicates(subset=["date"], keep="last").sort_values("date")
            logger.info("返回 %s 日线：缓存 %d 条 + 新增 %d 条", symbol, len(cached), len(new_result))
            return combined

        logger.info("获取 %s 日线完成，共 %d 条", symbol, len(new_result))
        return new_result

    # -----------------------------------------------------------------------

    def get_sector_list(self, sector_type: str = "concept") -> pd.DataFrame:
        """获取板块列表（使用同花顺数据源）

        参数:
            sector_type: "industry"(行业) 或 "concept"(概念)
        返回: DataFrame[sector_name, sector_code, sector_type]
        """
        logger.info("获取 %s 板块列表（同花顺源）", sector_type)

        if sector_type not in ("industry", "concept"):
            raise ValueError(f"不支持的 sector_type: {sector_type}，应为 'industry' 或 'concept'")

        if sector_type == "concept":
            raw = self._retry(ak.stock_board_concept_name_ths)
        else:
            raw = self._retry(ak.stock_board_industry_name_ths)

        df = raw.rename(columns={"name": "sector_name", "code": "sector_code"})
        df["sector_type"] = sector_type

        # 缓存
        self._conn.execute("DELETE FROM sector_list WHERE sector_type = ?", (sector_type,))
        self._conn.commit()
        df.to_sql("sector_list", self._conn, if_exists="append", index=False)
        self._conn.commit()

        logger.info("已获取 %d 个 %s 板块", len(df), sector_type)
        return df

    # -----------------------------------------------------------------------

    def get_sector_constituents(self, sector_name: str,
                                sector_type: str = "concept") -> pd.DataFrame:
        """获取板块成分股（从同花顺页面解析）

        返回: DataFrame[code, name]
        """
        logger.info("获取板块 '%s'(%s) 成分股", sector_name, sector_type)

        # 查缓存
        cached = self._sql_to_df(
            "SELECT code, name FROM sector_constituents WHERE sector_name = ? AND sector_type = ?",
            params=(sector_name, sector_type),
        )
        if not cached.empty:
            logger.info("从缓存加载板块 '%s' 成分股，共 %d 只", sector_name, len(cached))
            return cached

        # 查找同花顺板块代码
        sector_code = self._get_ths_sector_code(sector_name, sector_type)
        if not sector_code:
            logger.warning("未找到板块 '%s' 的同花顺代码", sector_name)
            return pd.DataFrame(columns=["code", "name"])

        # 组件URL：概念板块 / 行业板块
        if sector_type == "concept":
            url = f"http://q.10jqka.com.cn/gn/detail/code/{sector_code}/"
        else:
            url = f"http://q.10jqka.com.cn/thshy/detail/code/{sector_code}/"

        # 获取页面
        try:
            resp = requests.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }, timeout=15)
            resp.encoding = "gbk"
            resp.raise_for_status()
        except Exception as e:
            logger.warning("获取板块 '%s' 页面失败: %s", sector_name, e)
            return pd.DataFrame(columns=["code", "name"])

        # 解析成分股
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # 方法1: 找所有指向同花顺股票页面的链接 (<a href="http://stock.10jqka.com.cn/XXXXXX/">)
        rows = []
        seen_codes = set()
        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            # 匹配同花顺股票详情页链接 (stock.10jqka.com.cn/XXXXXX/)
            match = re.search(r"/(\d{6})/?$", href)
            if match and "stock.10jqka.com.cn" in href:
                code = match.group(1)
                name = a_tag.get_text(strip=True)
                if code not in seen_codes and name:
                    seen_codes.add(code)
                    rows.append({"code": code, "name": name})

        # 方法2: 如果方法1不work，兜底查找所有6位数字+附近文本
        if len(rows) < 5:
            logger.info("方法1解析出 %d 个，尝试方法2", len(rows))
            # 查找所有形如 >XXXXXX< 且前缀为A股代码的6位数字
            for match in re.finditer(r">(\d{6})<", html):
                code = match.group(1)
                # 排除深交所板块指数(885xxx/886xxx/887xxx)和920xxx非股票代码
                if code.startswith(("885", "886", "887", "888", "889", "920")):
                    continue
                if code[:2] in ("00", "30", "60", "68", "83", "87", "92", "43", "88") \
                        and code not in seen_codes:
                    seen_codes.add(code)
                    rows.append({"code": code, "name": ""})

        if not rows:
            logger.warning("板块 '%s' 页面未解析到成分股", sector_name)
            return pd.DataFrame(columns=["code", "name"])

        result = pd.DataFrame(rows)

        # 写缓存
        cache_df = result.copy()
        cache_df["sector_name"] = sector_name
        cache_df["sector_type"] = sector_type
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM sector_constituents WHERE sector_name = ? AND sector_type = ?",
                (sector_name, sector_type),
            )
        cache_df.to_sql("sector_constituents", self._conn, if_exists="append", index=False)
        self._conn.commit()

        logger.info("板块 '%s' 成分股共 %d 只", sector_name, len(result))
        return result[["code", "name"]]

    def _get_ths_sector_code(self, sector_name: str, sector_type: str) -> str:
        """从缓存或THS板块列表中查找同花顺板块代码"""
        # 先查缓存
        cached = self._sql_to_df(
            "SELECT sector_code FROM sector_list WHERE sector_name = ? AND sector_type = ?",
            params=(sector_name, sector_type),
        )
        if not cached.empty:
            code = cached.iloc[0]["sector_code"]
            if pd.notna(code) and str(code).strip():
                return str(code).strip()

        # 缓存没有，刷新板块列表
        df = self.get_sector_list(sector_type)
        match = df[df["sector_name"] == sector_name]
        if not match.empty:
            return str(match.iloc[0]["sector_code"])
        return ""

    # -----------------------------------------------------------------------

    def get_sector_daily(self, sector_name: str, sector_type: str,
                         start_date: str, end_date: str) -> pd.DataFrame:
        """获取板块历史日线行情（使用同花顺数据源）

        返回: DataFrame[date, close, change_pct]
        """
        logger.info("获取板块 '%s'(%s) 日线 %s ~ %s", sector_name, sector_type, start_date, end_date)

        # 格式化日期 (YYYYMMDD → YYYY-MM-DD) 用于SQL查询
        s_start = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        s_end = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

        # 先查缓存
        cached = self._sql_to_df(
            "SELECT date, close, change_pct FROM sector_daily "
            "WHERE sector_name = ? AND sector_type = ? "
            "AND date >= ? AND date <= ? ORDER BY date",
            params=(sector_name, sector_type, s_start, s_end),
        )
        if not cached.empty:
            logger.info("从缓存加载板块 '%s' 日线数据，共 %d 条", sector_name, len(cached))
            return cached

        # 调用同花顺板块日线接口
        try:
            if sector_type == "concept":
                raw = self._retry(
                    ak.stock_board_concept_index_ths,
                    symbol=sector_name,
                    start_date=start_date,
                    end_date=end_date,
                )
            else:
                raw = self._retry(
                    ak.stock_board_industry_index_ths,
                    symbol=sector_name,
                    start_date=start_date,
                    end_date=end_date,
                )
        except Exception as e:
            logger.warning("获取板块 '%s' 日线失败: %s", sector_name, e)
            return pd.DataFrame(columns=["date", "close", "change_pct"])

        if raw.empty:
            return pd.DataFrame(columns=["date", "close", "change_pct"])

        # 重命名列
        df = raw.rename(columns=_SECTOR_DAILY_COL_MAP)
        keep = ["date", "close"]
        for c in keep:
            if c not in df.columns:
                df[c] = None
        df = df[keep]
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")

        # 计算涨跌幅
        df["change_pct"] = df["close"].pct_change() * 100
        df["change_pct"] = df["change_pct"].fillna(0.0)

        # 缓存
        cache_df = df.copy()
        cache_df["sector_name"] = sector_name
        cache_df["sector_type"] = sector_type
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM sector_daily WHERE sector_name = ? AND sector_type = ? AND date >= ? AND date <= ?",
                (sector_name, sector_type, s_start, s_end),
            )
        cache_df.to_sql("sector_daily", self._conn, if_exists="append", index=False)
        self._conn.commit()

        return df

    # -----------------------------------------------------------------------

    def get_profit_data(self, symbol: str) -> pd.DataFrame:
        """获取某只股票的年度净利润数据

        参数:
            symbol: 纯数字股票代码（如 600519），内部会自动加 SH/SZ 前缀
        返回: DataFrame[report_date, net_profit]
        """
        prefixed = code_to_ths_prefix(symbol)
        logger.info("获取 %s(%s) 年度利润数据", symbol, prefixed)

        # 查缓存
        cached = self._sql_to_df(
            "SELECT year, net_profit FROM profit_data WHERE code = ? ORDER BY year",
            params=(symbol,),
        )
        if not cached.empty:
            cached = cached.rename(columns={"year": "report_date"})
            cached["report_date"] = cached["report_date"].apply(
                lambda y: f"{int(y)}-12-31" if pd.notna(y) else None
            )
            logger.info("从缓存加载 %s 利润数据", symbol)
            return cached

        try:
            raw = self._retry(ak.stock_profit_sheet_by_yearly_em, symbol=prefixed)
        except Exception as e:
            logger.error("获取 %s 利润数据失败: %s", symbol, e)
            return pd.DataFrame(columns=["report_date", "net_profit"])

        df = pd.DataFrame()

        # 报告期
        if "REPORT_DATE" in raw.columns:
            df["report_date"] = raw["REPORT_DATE"]
        elif "报告期" in raw.columns:
            df["report_date"] = raw["报告期"]
        else:
            df["report_date"] = raw.iloc[:, 0]

        # 归母净利润（优先）→ 净利润
        if "PARENT_NETPROFIT" in raw.columns:
            df["net_profit"] = raw["PARENT_NETPROFIT"]
        elif "NETPROFIT" in raw.columns:
            df["net_profit"] = raw["NETPROFIT"]
        elif "净利润" in raw.columns:
            df["net_profit"] = raw["净利润"]
        else:
            profit_cols = [c for c in raw.columns if "净利润" in str(c)]
            if profit_cols:
                df["net_profit"] = raw[profit_cols[0]]
            else:
                logger.warning("%s 利润数据中未找到净利润列，可用列: %s",
                               symbol, raw.columns.tolist()[:10])
                return pd.DataFrame(columns=["report_date", "net_profit"])

        df["net_profit"] = pd.to_numeric(df["net_profit"], errors="coerce")
        df = df.dropna(subset=["net_profit"])

        df["report_date"] = pd.to_datetime(df["report_date"]).dt.strftime("%Y-%m-%d")

        # 提取年份并缓存
        df["year"] = pd.to_datetime(df["report_date"]).dt.year
        cache_df = df[["year", "net_profit"]].copy()
        cache_df["code"] = symbol
        cache_df = cache_df[["code", "year", "net_profit"]]

        with self._cursor() as cur:
            cur.execute("DELETE FROM profit_data WHERE code = ?", (symbol,))
        cache_df.to_sql("profit_data", self._conn, if_exists="append", index=False)
        self._conn.commit()

        df = df[["report_date", "net_profit"]]
        logger.info("获取 %s 利润数据完成，共 %d 条", symbol, len(df))
        return df
