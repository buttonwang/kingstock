"""回测引擎 - 使用与选股系统完全相同的筛选逻辑进行历史回测

原理：
  1. 预加载阶段：将所有板块日线和候选股票日线数据拉取到 SQLite 缓存
  2. 内存回放阶段：对每个交易日，从内存中切片数据，调用与选股系统
     相同的筛选函数（RPS / MACD / ZJTJ / KDJ），重建当天的选股结果
  3. 收益计算：利用日线数据中未来 N 个交易日的收盘价计算涨幅

回测只验证「主选股 MACD ∩ ZJTJ」核心条件，手工选股和 EBK 采用相同的
MACD+ZJTJ 条件从 RPS 池中筛选（简化处理，因为手工/EBK 依赖外部分析文件）。
"""

import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd
import numpy as np

# 确保项目根目录在 sys.path 中
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.data_fetcher import DataFetcher
from src.filters.sector_filter import filter_by_sector_rps
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.scoring import compute_total_score
from src.ml import ml_scorer
from src.indicators.macd import calculate_macd
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from config.settings import (
    RPS_PERIOD, RPS_TOP_N, SECTOR_TYPE,
    PROFIT_GROWTH_YEARS, PROFIT_GROWTH_MIN,
    OUTPUT_PATH,
    VOLUME_CONFIRM_ENABLED, MA_ALIGN_ENABLED, PRICE_POSITION_ENABLED,
    ML_SCORE_ENABLED,
)
from src.utils import setup_logging
from src.backtest.position_manager import PositionManager

logger = setup_logging("backtest_engine")

# 未来考察窗口（交易日）
FORWARD_WINDOWS = [2, 10, 30, 60]


class BacktestEngine:
    """回测引擎

    用法:
        engine = BacktestEngine(start_date="20250601", end_date="20260601")
        engine.preload_all_data()     # 首次需要，之后可跳过
        engine.run()                  # 执行回测
        df = engine.get_results()     # 获取详细结果
    """

    def __init__(self, start_date: str = None, end_date: str = None):
        """初始化回测引擎

        参数:
            start_date: 回测起始日 YYYYMMDD（默认 1 年前）
            end_date:   回测截止日 YYYYMMDD（默认今天）
        """
        now = datetime.now()
        self.end_date = end_date or now.strftime("%Y%m%d")
        if start_date is None:
            start_dt = now - timedelta(days=370)
            self.start_date = start_dt.strftime("%Y%m%d")
        else:
            self.start_date = start_date

        # 数据获取需要更宽的时间范围
        # lookback: 指标计算需要 200 天历史，再加 RPS 周期余量
        lookback_dt = pd.Timestamp(self.start_date) - pd.Timedelta(days=250)
        self._fetch_start = lookback_dt.strftime("%Y%m%d")
        # lookforward: 60 个交易日 ≈ 120 自然日
        lookfwd_dt = pd.Timestamp(self.end_date) + pd.Timedelta(days=120)
        now = datetime.now()
        # 当end_date接近现在时，不拉取超出当前日期太多的未来数据
        max_fetch = min(
            lookfwd_dt,
            now + pd.Timedelta(days=10),
        )
        self._fetch_end = max_fetch.strftime("%Y%m%d")

        self.fetcher = DataFetcher()

        # ---- 内存缓存 ----
        self.sector_daily_cache = {}       # {sector_name: DataFrame}
        self.sector_constituents_cache = {}  # {sector_name: set of codes}
        self.stock_daily_cache = {}        # {code: DataFrame}
        self.all_sectors = []              # 板块名称列表
        self.stock_name_map = {}           # {code: name}
        self.trading_dates = []            # 有序的交易日列表（用于回放）

        # ---- 回测结果 ----
        self._results = []                 # 逐条记录

    # ------------------------------------------------------------------
    # 预加载数据
    # ------------------------------------------------------------------

    def preload_all_data(self):
        """预加载阶段：一次性将所有需要的数据写入 SQLite 缓存

        后续的回放阶段完全从缓存读取，不再触发 API 请求。
        """
        from tqdm import tqdm

        logger.info("=" * 60)
        logger.info("开始预加载数据")
        logger.info("回测范围: %s ~ %s", self.start_date, self.end_date)
        logger.info("数据范围: %s ~ %s", self._fetch_start, self._fetch_end)
        logger.info("=" * 60)

        # 1. 股票列表
        logger.info("[1/4] 加载股票列表...")
        stocks = self.fetcher.get_all_stocks()
        self.stock_name_map = dict(zip(stocks["code"], stocks["name"]))
        logger.info("共 %d 只股票", len(stocks))

        # 2. 板块列表 + 日线
        logger.info("[2/4] 加载板块日线数据...")
        sectors = self.fetcher.get_sector_list(SECTOR_TYPE)
        self.all_sectors = sectors["sector_name"].tolist()
        logger.info("共 %d 个板块（%s）", len(self.all_sectors), SECTOR_TYPE)

        for name in tqdm(self.all_sectors, desc="板块日线"):
            try:
                df = self.fetcher.get_sector_daily(
                    name, SECTOR_TYPE, self._fetch_start, self._fetch_end,
                )
                if df is not None and not df.empty:
                    self.sector_daily_cache[name] = df
            except Exception as e:
                logger.debug("板块 %s 日线获取失败: %s", name, e)

        logger.info("成功加载 %d 个板块日线", len(self.sector_daily_cache))

        # 3. 板块成分股
        logger.info("[3/4] 加载板块成分股...")
        all_codes = set()
        for name in tqdm(self.all_sectors, desc="成分股"):
            try:
                cons = self.fetcher.get_sector_constituents(name, SECTOR_TYPE)
                if cons is not None and not cons.empty:
                    codes = set(cons["code"].tolist())
                    self.sector_constituents_cache[name] = codes
                    all_codes.update(codes)
            except Exception as e:
                logger.debug("板块 %s 成分股获取失败: %s", name, e)

        logger.info("候选池共 %d 只股票（去重后）", len(all_codes))

        # 4. 候选股票日线
        logger.info("[4/4] 加载候选股票日线数据...")
        codes_list = sorted(all_codes)
        for code in tqdm(codes_list, desc="股票日线"):
            try:
                df = self.fetcher.get_stock_daily(
                    code, self._fetch_start, self._fetch_end,
                )
                if df is not None and not df.empty:
                    self.stock_daily_cache[code] = df
            except Exception:
                pass

        logger.info("成功加载 %d 只股票的日线数据", len(self.stock_daily_cache))

        # 5. 提取交易日列表（从板块日线中获取）
        self.trading_dates = self._extract_trading_dates()
        logger.info("交易日: %d 天（%s ~ %s）",
                     len(self.trading_dates),
                     self.trading_dates[0] if self.trading_dates else "N/A",
                     self.trading_dates[-1] if self.trading_dates else "N/A")
        logger.info("=" * 60)
        logger.info("预加载完成")
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 交易日处理
    # ------------------------------------------------------------------

    def _extract_trading_dates(self) -> list:
        """从板块日线数据中提取有序的交易日列表（限定回测范围内）"""
        all_dates = set()
        for df in self.sector_daily_cache.values():
            all_dates.update(df["date"].tolist())

        s_start = pd.Timestamp(self.start_date)
        s_end = pd.Timestamp(self.end_date)

        dates = sorted(
            d for d in all_dates
            if s_start <= pd.Timestamp(d) <= s_end
        )
        return dates

    def _get_data_for_date(self, date_str: str) -> tuple:
        """获取模拟指定交易日所需的内存数据切片

        返回:
            (sector_data, candidate_codes, top_sectors, rps_df)
            sector_data: {sector_name: DataFrame filtered up to date_str}
            candidate_codes: set of stock codes in RPS candidate pool
            top_sectors: list of top sector names
            rps_df: RPS DataFrame with sector rankings
        """
        fmt = pd.Timestamp(date_str)

        # 切片板块日线：取 date_str 及之前的数据
        sector_data = {}
        for name, df in self.sector_daily_cache.items():
            mask = pd.to_datetime(df["date"]) <= fmt
            sub = df[mask].copy()
            if not sub.empty and len(sub) >= RPS_PERIOD:
                sector_data[name] = sub

        # 获取 RPS 前 N 板块的成分股
        try:
            rps_df = calculate_sector_rps(sector_data, period=RPS_PERIOD)
            top_sectors = get_top_sectors(rps_df, top_n=RPS_TOP_N)
        except Exception:
            return sector_data, set(), [], pd.DataFrame()

        candidate_codes = set()
        for name in top_sectors:
            codes = self.sector_constituents_cache.get(name, set())
            candidate_codes.update(codes)

        return sector_data, candidate_codes, top_sectors, rps_df

    # ------------------------------------------------------------------
    # 模拟选股
    # ------------------------------------------------------------------

    def _simulate_date(self, date_str: str) -> dict:
        """对单个交易日模拟选股（含增强规则检查与评分）

        返回:
            {code: {"main": bool, ..., "score": dict, "enhanced_rules": dict}}
        """
        fmt_date = pd.Timestamp(date_str)
        result = {}

        # 1. RPS 筛选候选池
        _, candidate_codes, top_sectors, rps_df = self._get_data_for_date(date_str)
        if not candidate_codes:
            return result

        # 构建 股票→板块 映射（用于RPS评分）
        code_to_sector = {}
        for name in top_sectors:
            codes = self.sector_constituents_cache.get(name, set())
            for c in codes:
                if c not in code_to_sector:
                    code_to_sector[c] = name
        # 构建 板块→RPS排名 映射
        sector_rank_map = {}
        if not rps_df.empty:
            for _, row in rps_df.iterrows():
                sector_rank_map[row['sector_name']] = row['rps_rank']

        # 2. 构建候选股票日线字典（截止选股日）
        stock_dict = {}
        for code in candidate_codes:
            df = self.stock_daily_cache.get(code)
            if df is None or df.empty:
                continue
            mask = pd.to_datetime(df["date"]) <= fmt_date
            sub = df[mask].copy()
            if not sub.empty and len(sub) >= 60:  # 至少 60 个交易日
                stock_dict[code] = sub

        if not stock_dict:
            return result

        # 3. MACD / ZJTJ 并行筛选
        macd_codes = filter_by_macd(stock_dict)
        zjtj_codes = filter_by_zjtj(stock_dict)
        kdj_codes = filter_by_kdj(stock_dict)

        core_codes = macd_codes & zjtj_codes

        # 4. 记录结果（主选股 = MACD ∩ ZJTJ）
        for code in core_codes:
            kdj_pass = code in kdj_codes

            # 计算增强规则、评分和ML评分
            df = stock_dict.get(code)
            score_info = {"total_score": 0, "score_macd": 0, "score_zjtj": 0,
                          "score_kdj": 0, "score_rps": 0, "score_volume": 0,
                          "score_finance": 0, "score_ml": 0,
                          "volume_pass": False,
                          "ma_alignment_pass": False, "price_position_pass": False}

            if df is not None and len(df) >= 60:
                try:
                    df_macd = calculate_macd(df)
                    df_kdj = calculate_kdj(df)
                    df_zjtj = calculate_zjtj(df)
                    enh = check_all_enhanced_rules(df)
                    sector = code_to_sector.get(code, "")
                    rps_rank = sector_rank_map.get(sector, RPS_TOP_N)

                    # ML评分
                    ml_val = None
                    if ML_SCORE_ENABLED and ml_scorer.is_available():
                        try:
                            ml_val = ml_scorer.predict_score(
                                df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N,
                            )
                        except Exception:
                            ml_val = None

                    scores = compute_total_score(
                        df_macd, df_kdj, df_zjtj, rps_rank,
                        ml_score=ml_val,
                    )
                    score_info.update(scores)
                    score_info.update(enh)
                except Exception:
                    pass

            result[code] = {
                "main": True, "manual": True, "ebk": True,
                "kdj_pass": kdj_pass, **score_info,
            }

        return result

    # ------------------------------------------------------------------
    # 收益计算
    # ------------------------------------------------------------------

    def _compute_forward_returns(self, code: str, sel_date_str: str) -> dict:
        """计算选股日后 N 个交易日的涨幅

        参数:
            code: 股票代码
            sel_date_str: 选股日 YYYYMMDD

        返回:
            {2: pct, 10: pct, 30: pct, 60: pct} 值可能为 None
        """
        df = self.stock_daily_cache.get(code)
        if df is None or df.empty:
            return {w: None for w in FORWARD_WINDOWS}

        # 排序并重置索引
        df = df.sort_values("date").reset_index(drop=True)
        fmt = pd.Timestamp(sel_date_str)

        # 找到选股日所在的索引（取当天或最接近的后一天）
        dates = pd.to_datetime(df["date"])
        sel_idx = None
        for i, d in enumerate(dates):
            if d >= fmt:
                sel_idx = i
                break

        if sel_idx is None:
            return {w: None for w in FORWARD_WINDOWS}

        closes = df["close"].values
        base_close = closes[sel_idx]
        if base_close == 0:
            return {w: None for w in FORWARD_WINDOWS}

        result = {}
        for n in FORWARD_WINDOWS:
            fwd_idx = sel_idx + n
            if fwd_idx < len(closes):
                fwd_close = closes[fwd_idx]
                result[n] = round((fwd_close / base_close - 1) * 100, 2)
            else:
                result[n] = None

        return result

    # ------------------------------------------------------------------
    # 止损止盈模拟
    # ------------------------------------------------------------------

    def _simulate_stop_profit(self, code: str, sel_date_str: str, total_score: int = -1) -> dict:
        """模拟单笔持仓的止损止盈过程

        参数:
            code: 股票代码
            sel_date_str: 选股日 YYYYMMDD
            total_score: 综合评分，用于分段止损（-1=不启用）

        返回:
            {"sl_tp_return": 已实现收益率%, "sl_tp_hold_days": 持仓天数,
             "sl_tp_exit_reason": 退出原因代码}
        """
        df = self.stock_daily_cache.get(code)
        if df is None or df.empty:
            return {"sl_tp_return": None, "sl_tp_hold_days": None,
                    "sl_tp_exit_reason": None}

        df = df.sort_values("date").reset_index(drop=True)
        dates = pd.to_datetime(df["date"])
        fmt = pd.Timestamp(sel_date_str)

        # 找到选股日索引
        sel_idx = None
        for i, d in enumerate(dates):
            if d >= fmt:
                sel_idx = i
                break
        if sel_idx is None:
            return {"sl_tp_return": None, "sl_tp_hold_days": None,
                    "sl_tp_exit_reason": None}

        closes = df["close"].values
        entry_price = closes[sel_idx]
        if entry_price is None or entry_price <= 0:
            return {"sl_tp_return": None, "sl_tp_hold_days": None,
                    "sl_tp_exit_reason": None}

        pm = PositionManager(entry_price, sel_date_str, total_score=total_score)

        # 遍历未来交易日（最多60天）
        max_lookahead = 60
        for offset in range(1, max_lookahead + 1):
            fwd_idx = sel_idx + offset
            if fwd_idx >= len(closes):
                break
            current_price = closes[fwd_idx]
            pm.update(current_price)
            if pm.is_closed:
                break

        summary = pm.get_summary()
        return {
            "sl_tp_return": summary["total_return"],
            "sl_tp_hold_days": summary["hold_days"],
            "sl_tp_exit_reason": summary["exit_reason"],
        }

    # ------------------------------------------------------------------
    # 基准计算
    # ------------------------------------------------------------------

    def _compute_benchmark(self, date_str: str) -> dict:
        """计算同期全市场平均涨幅作为基准

        取所有有日线数据的股票在 date_str 的平均涨幅，
        作为朴素买入持有基准。
        """
        fmt = pd.Timestamp(date_str)
        all_pct_2d = []
        all_pct_10d = []
        all_pct_30d = []
        all_pct_60d = []

        # 只取候选池中的股票作为基准（与选股同范围）
        for code, df in self.stock_daily_cache.items():
            if df.empty:
                continue
            df_sorted = df.sort_values("date").reset_index(drop=True)
            dates = pd.to_datetime(df_sorted["date"])
            sel_idx = None
            for i, d in enumerate(dates):
                if d >= fmt:
                    sel_idx = i
                    break
            if sel_idx is None:
                continue

            closes = df_sorted["close"].values
            base_close = closes[sel_idx]
            if base_close == 0:
                continue

            for n, lst in [(2, all_pct_2d), (10, all_pct_10d),
                           (30, all_pct_30d), (60, all_pct_60d)]:
                fwd_idx = sel_idx + n
                if fwd_idx < len(closes):
                    pct = round((closes[fwd_idx] / base_close - 1) * 100, 2)
                    lst.append(pct)

        def _avg(lst):
            return round(np.mean(lst), 2) if lst else None

        return {
            "bench_2d": _avg(all_pct_2d),
            "bench_10d": _avg(all_pct_10d),
            "bench_30d": _avg(all_pct_30d),
            "bench_60d": _avg(all_pct_60d),
        }

    # ------------------------------------------------------------------
    # 主运行循环
    # ------------------------------------------------------------------

    def run(self):
        """执行回测模拟

        遍历所有交易日，对每个日期模拟选股并计算后续收益。
        """
        from tqdm import tqdm

        # 如果没预加载过，尝试从缓存读取
        if not self.trading_dates:
            self._load_from_cache()

        if not self.trading_dates:
            logger.warning("无交易日数据，请先调用 preload_all_data()")
            return

        logger.info("=" * 60)
        logger.info("开始回测模拟")
        logger.info("交易日: %d 天", len(self.trading_dates))
        logger.info("=" * 60)

        total_signals = 0

        for date_str in tqdm(self.trading_dates, desc="回测"):
            # 模拟选股
            selected = self._simulate_date(date_str)
            if not selected:
                continue

            # 基准
            bench = self._compute_benchmark(date_str)

            # 对每只选中的股票计算未来收益
            for code, info in selected.items():
                returns = self._compute_forward_returns(code, date_str)

                # 止损止盈模拟
                sl_tp_result = self._simulate_stop_profit(code, date_str, total_score=info.get("total_score", -1))

                record = {
                    "date": date_str,
                    "code": code,
                    "name": self.stock_name_map.get(code, ""),
                    "source_main": info["main"],
                    "source_manual": info["manual"],
                    "source_ebk": info["ebk"],
                    "kdj_pass": info["kdj_pass"],
                    "total_score": info.get("total_score", 0),
                    "score_macd": info.get("score_macd", 0),
                    "score_zjtj": info.get("score_zjtj", 0),
                    "score_kdj": info.get("score_kdj", 0),
                    "score_rps": info.get("score_rps", 0),
                    "score_volume": info.get("score_volume", 0),
                    "score_finance": info.get("score_finance", 0),
                    "score_ml": info.get("score_ml", 0),
                    "volume_pass": info.get("volume_pass", False),
                    "ma_alignment_pass": info.get("ma_alignment_pass", False),
                    "price_position_pass": info.get("price_position_pass", False),
                    "sl_tp_return": sl_tp_result.get("sl_tp_return"),
                    "sl_tp_hold_days": sl_tp_result.get("sl_tp_hold_days"),
                    "sl_tp_exit_reason": sl_tp_result.get("sl_tp_exit_reason"),
                }
                for n in FORWARD_WINDOWS:
                    record[f"return_{n}d"] = returns.get(n)
                    record[f"bench_{n}d"] = bench.get(f"bench_{n}d")

                self._results.append(record)
                total_signals += 1

        logger.info("回测完成: 共 %d 条信号记录", total_signals)
        logger.info("=" * 60)

    # ------------------------------------------------------------------
    # 结果处理
    # ------------------------------------------------------------------

    def _load_from_cache(self):
        """尝试从现有缓存中加载数据（跳过预加载的快速启动）"""
        logger.info("从缓存加载已有数据...")

        # 加载股票列表
        try:
            stocks = self.fetcher.get_all_stocks()
            self.stock_name_map = dict(zip(stocks["code"], stocks["name"]))
        except Exception:
            pass

        # 加载板块列表
        try:
            sectors = self.fetcher.get_sector_list(SECTOR_TYPE)
            self.all_sectors = sectors["sector_name"].tolist()
        except Exception:
            pass

        # 读取板块日线缓存
        try:
            for name in self.all_sectors:
                df = self.fetcher.get_sector_daily(
                    name, SECTOR_TYPE, self._fetch_start, self._fetch_end,
                )
                if df is not None and not df.empty:
                    self.sector_daily_cache[name] = df
        except Exception:
            pass

        # 读取板块成分股
        try:
            for name in self.all_sectors:
                cons = self.fetcher.get_sector_constituents(name, SECTOR_TYPE)
                if cons is not None and not cons.empty:
                    codes = set(cons["code"].tolist())
                    self.sector_constituents_cache[name] = codes
        except Exception:
            pass

        # 读取所有缓存的股票日线
        try:
            all_codes = set()
            for codes in self.sector_constituents_cache.values():
                all_codes.update(codes)
            for code in all_codes:
                df = self.fetcher.get_stock_daily(
                    code, self._fetch_start, self._fetch_end,
                )
                if df is not None and not df.empty:
                    self.stock_daily_cache[code] = df
        except Exception:
            pass

        # 提取交易日
        self.trading_dates = self._extract_trading_dates()
        logger.info("从缓存加载: %d 板块, %d 股票, %d 交易日",
                     len(self.sector_daily_cache),
                     len(self.stock_daily_cache),
                     len(self.trading_dates))

    def get_results(self) -> pd.DataFrame:
        """获取回测详细结果 DataFrame"""
        if not self._results:
            return pd.DataFrame()
        return pd.DataFrame(self._results)

    # ------------------------------------------------------------------
    # 统计分析
    # ------------------------------------------------------------------

    def summarize(self) -> dict:
        """生成回测统计摘要

        返回:
            {source_name: {metric: value}}
            source_name: "主选股" / "手工选股" / "龙头EBK"
        """
        if not self._results:
            logger.warning("无回测结果，请先调用 run()")
            return {}

        df = self.get_results()
        summaries = {}

        for source_key, source_label in [
            ("source_main", "主选股"),
            ("source_manual", "手工选股"),
            ("source_ebk", "龙头EBK"),
        ]:
            sub = df[df[source_key]].copy()
            if sub.empty:
                summaries[source_label] = {"signal_count": 0}
                continue

            stats = {
                "signal_count": len(sub),
                "stock_count": sub["code"].nunique(),
                "date_count": sub["date"].nunique(),
            }

            for n in FORWARD_WINDOWS:
                col = f"return_{n}d"
                vals = sub[col].dropna()
                if vals.empty:
                    stats[f"avg_return_{n}d"] = None
                    stats[f"win_rate_{n}d"] = None
                    stats[f"max_return_{n}d"] = None
                    stats[f"min_return_{n}d"] = None
                    stats[f"bench_{n}d"] = None
                else:
                    stats[f"avg_return_{n}d"] = round(vals.mean(), 2)
                    stats[f"win_rate_{n}d"] = round(
                        (vals > 0).sum() / len(vals) * 100, 1
                    )
                    stats[f"max_return_{n}d"] = round(vals.max(), 2)
                    stats[f"min_return_{n}d"] = round(vals.min(), 2)
                    # 基准
                    bench_vals = sub[f"bench_{n}d"].dropna()
                    stats[f"bench_{n}d"] = round(bench_vals.mean(), 2) if not bench_vals.empty else None
                    # 跑赢基准比例
                    both = pd.concat([vals, bench_vals], axis=1, keys=["sel", "bench"]).dropna()
                    if not both.empty:
                        beat = (both["sel"] > both["bench"]).sum()
                        stats[f"beat_bench_{n}d"] = round(beat / len(both) * 100, 1)

            # 止损止盈统计（含增强指标）
            sl_vals = sub["sl_tp_return"].dropna()
            if not sl_vals.empty:
                stats["sl_tp_avg_return"] = round(sl_vals.mean(), 2)
                stats["sl_tp_win_rate"] = round(
                    (sl_vals > 0).sum() / len(sl_vals) * 100, 1
                )
                stats["sl_tp_max_return"] = round(sl_vals.max(), 2)
                stats["sl_tp_min_return"] = round(sl_vals.min(), 2)
                stats["sl_tp_avg_hold_days"] = round(
                    sub["sl_tp_hold_days"].dropna().mean(), 1
                )

                # V2: 夏普比率 (年化)
                sl_std = sl_vals.std()
                if sl_std > 0:
                    # 假设平均每个信号持仓约45天 (年化 252/45 ≈ 5.6)
                    trades_per_year = max(252.0 / max(stats["sl_tp_avg_hold_days"], 1), 1)
                    annual_return = sl_vals.mean() * trades_per_year / 100  # 转为小数
                    annual_vol = sl_std * np.sqrt(trades_per_year) / 100
                    stats["sl_tp_sharpe"] = round(annual_return / annual_vol, 2) if annual_vol > 0 else 0.0
                else:
                    stats["sl_tp_sharpe"] = None

                # V2: 最大回撤 (基于sl_tp_return模拟净值)
                cum_returns = sl_vals.values / 100.0  # 转为小数
                net_worth = (1 + cum_returns).cumprod()
                rolling_max = np.maximum.accumulate(net_worth)
                drawdowns = (net_worth - rolling_max) / rolling_max
                stats["sl_tp_max_dd"] = round(abs(drawdowns.min()) * 100, 2) if len(drawdowns) > 0 else 0.0

                # V2: 盈亏比
                wins = sl_vals[sl_vals > 0]
                losses = sl_vals[sl_vals < 0]
                avg_win = wins.mean() if not wins.empty else 0
                avg_loss = abs(losses.mean()) if not losses.empty else 0
                stats["sl_tp_profit_loss_ratio"] = round(avg_win / avg_loss, 2) if avg_loss > 0 else None

                # V2: Calmar比率
                max_dd = stats.get("sl_tp_max_dd", 0)
                if max_dd > 0:
                    trades_per_year = max(252.0 / max(stats["sl_tp_avg_hold_days"], 1), 1)
                    annual_return_pct = sl_vals.mean() * trades_per_year
                    stats["sl_tp_calmar"] = round(annual_return_pct / max_dd, 2)
                else:
                    stats["sl_tp_calmar"] = None

                # 退出原因分布
                reason_counts = sub["sl_tp_exit_reason"].value_counts()
                stats["exit_reasons"] = reason_counts.to_dict()

                # V2: 分段统计 (按total_score)
                if "total_score" in sub.columns:
                    buckets = [
                        (0, 59, "低分(<60)"),
                        (60, 69, "中低分(60~69)"),
                        (70, 79, "中分(70~79)"),
                        (80, 89, "中高分(80~89)"),
                        (90, 100, "高分(>=90)"),
                    ]
                    bucket_rows = []
                    for lo, hi, label in buckets:
                        mask = (sub["total_score"] >= lo) & (sub["total_score"] <= hi)
                        bucket = sub[mask]
                        if bucket.empty:
                            continue
                        b_sl = bucket["sl_tp_return"].dropna()
                        b_hold = bucket["sl_tp_hold_days"].dropna().mean()
                        bucket_rows.append({
                            "分段": label,
                            "信号数": len(bucket),
                            "sl_tp均收益": round(b_sl.mean(), 2) if not b_sl.empty else None,
                            "sl_tp胜率": round((b_sl > 0).sum() / len(b_sl) * 100, 1) if not b_sl.empty else None,
                            "均持仓天": round(b_hold, 1) if not pd.isna(b_hold) else None,
                        })
                    stats["score_buckets"] = bucket_rows
            else:
                stats["sl_tp_avg_return"] = None
                stats["sl_tp_win_rate"] = None

            summaries[source_label] = stats

        return summaries

    def print_summary(self):
        """打印回测统计摘要到终端"""
        summaries = self.summarize()
        if not summaries:
            return

        header = f"{'来源':<10} {'信号数':>6} {'股票数':>6} {'天数':>6}"
        for n in FORWARD_WINDOWS:
            header += f"  {'均涨'+str(n)+'d':>9} {'胜率'+str(n)+'d':>7} {'跑赢'+str(n)+'d':>7}"
        print()
        print("=" * (len(header) + 20))
        print("  选股逻辑回测结果")
        print(f"  回测区间: {self.start_date[:4]}-{self.start_date[4:6]}-{self.start_date[6:8]}"
              f" ~ {self.end_date[:4]}-{self.end_date[4:6]}-{self.end_date[6:8]}")
        print("=" * (len(header) + 20))
        print(header)
        print("-" * len(header))

        for src_label, stats in summaries.items():
            if stats.get("signal_count", 0) == 0:
                print(f"{src_label:<10} {'无信号':>6}")
                continue
            line = f"{src_label:<10} {stats['signal_count']:>6} {stats['stock_count']:>6} {stats['date_count']:>6}"
            for n in FORWARD_WINDOWS:
                avg = stats.get(f"avg_return_{n}d")
                win = stats.get(f"win_rate_{n}d")
                beat = stats.get(f"beat_bench_{n}d")
                avg_s = f"{avg:+.2f}%" if avg is not None else "  N/A  "
                win_s = f"{win:.1f}%" if win is not None else "  N/A"
                beat_s = f"{beat:.1f}%" if beat is not None else "  N/A"
                line += f"  {avg_s:>9} {win_s:>7} {beat_s:>7}"
            print(line)
        print("-" * len(header))

        # 止损止盈行
        for src_label, stats in summaries.items():
            sl_avg = stats.get("sl_tp_avg_return")
            sl_win = stats.get("sl_tp_win_rate")
            sl_hold = stats.get("sl_tp_avg_hold_days")
            sl_sharpe = stats.get("sl_tp_sharpe")
            sl_mdd = stats.get("sl_tp_max_dd")
            sl_pl = stats.get("sl_tp_profit_loss_ratio")
            sl_calmar = stats.get("sl_tp_calmar")
            if sl_avg is not None:
                sl_text = f"{src_label:<10} {'止损止盈':>6} {'':>6} {'':>6}"
                sl_text += f"  {sl_avg:+.2f}%{'':>7} {sl_win:.1f}%{'':>7} {'':>7}"
                print(sl_text)

                # V2: 夏普 / 最大回撤 / 盈亏比 / Calmar
                extra_parts = []
                if sl_sharpe is not None:
                    extra_parts.append(f"夏普:{sl_sharpe:.2f}")
                if sl_mdd is not None:
                    extra_parts.append(f"最大回撤:{sl_mdd:.1f}%")
                if sl_pl is not None:
                    extra_parts.append(f"盈亏比:{sl_pl:.2f}")
                if sl_calmar is not None:
                    extra_parts.append(f"Calmar:{sl_calmar:.2f}")
                if extra_parts:
                    print(f"{'':<10} {'风控指标':>6} {' | '.join(extra_parts)}")

                reasons = stats.get("exit_reasons", {})
                if reasons:
                    total = sum(reasons.values())
                    parts = []
                    for r in ["TAKE_PROFIT_10", "TAKE_PROFIT_20", "TAKE_PROFIT_30",
                              "TAKE_PROFIT_15", "TAKE_PROFIT_25", "TAKE_PROFIT_50",
                              "HARD_STOP_LOSS", "STOP_LOSS", "EXPIRED"]:
                        cnt = reasons.get(r, 0)
                        if cnt > 0:
                            pct = cnt / total * 100
                            label = r.replace("TAKE_PROFIT_", "TP").replace("HARD_STOP_LOSS", "HSL")
                            parts.append(f"{label}:{pct:.0f}%")
                    print(f"{'':<10} {'退出分布':>6} {', '.join(parts)}")
                if sl_hold is not None:
                    print(f"{'':<10} {'均持仓':>6} {sl_hold:.1f}天")

            # V2: 分段统计展示
            buckets = stats.get("score_buckets", [])
            if buckets:
                print(f"{'':<10} {'---分段统计---':>6}")
                for b in buckets:
                    print(f"{'':<12} {b['分段']}: {b['信号数']}次 收益{b['sl_tp均收益']:+.2f}% "
                          f"胜率{b['sl_tp胜率']:.1f}% 均持仓{b['均持仓天']:.0f}天"
                          if b['sl_tp均收益'] is not None else f"{b['分段']}: {b['信号数']}次")
                print()
        print("-" * len(header))

        # 基准对比行
        if self._results:
            df = self.get_results()
            bench_line = f"{'全市场':<10} {'---':>6} {'---':>6} {'---':>6}"
            for n in FORWARD_WINDOWS:
                col = f"bench_{n}d"
                vals = df[col].dropna()
                if not vals.empty:
                    avg = round(vals.mean(), 2)
                    bench_line += f"  {avg:+.2f}%{'':>7} {'':>7}"
                else:
                    bench_line += f"  {'N/A':>9} {'':>7} {'':>7}"
            print(bench_line)
        print("=" * (len(header) + 20))

    def save_results(self):
        """保存回测详细结果到 CSV"""
        df = self.get_results()
        if df.empty:
            logger.warning("无结果可保存")
            return

        os.makedirs(OUTPUT_PATH, exist_ok=True)
        range_str = f"{self.start_date}_{self.end_date}"
        csv_path = os.path.join(OUTPUT_PATH, f"backtest_{range_str}.csv")
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        logger.info("回测详细结果已导出: %s", csv_path)

        # 摘要 CSV
        summaries = self.summarize()
        rows = []
        for src_label, stats in summaries.items():
            row = {"来源": src_label}
            for k, v in stats.items():
                if k == "exit_reasons":
                    # 退出分布展平为列
                    for reason, cnt in v.items():
                        row[f"exit_{reason}"] = cnt
                elif k == "score_buckets":
                    # 分段统计展平
                    for i, b in enumerate(v):
                        for bk, bv in b.items():
                            row[f"seg{i}_{bk}"] = bv
                elif k not in ("exit_reasons", "score_buckets"):
                    row[k] = v
            rows.append(row)

        if rows:
            summary_df = pd.DataFrame(rows)
            summary_path = os.path.join(OUTPUT_PATH, f"backtest_summary_{range_str}.csv")
            summary_df.to_csv(summary_path, index=False, encoding="utf-8-sig")
            logger.info("回测摘要已导出: %s", summary_path)

        # 退出分布 CSV
        exit_rows = []
        for _, row in df.iterrows():
            reason = row.get("sl_tp_exit_reason")
            if reason and reason not in ("OPEN", None):
                exit_rows.append({"date": row["date"], "code": row["code"],
                                  "name": row.get("name", ""),
                                  "exit_reason": reason,
                                  "sl_tp_return": row.get("sl_tp_return"),
                                  "sl_tp_hold_days": row.get("sl_tp_hold_days")})
        if exit_rows:
            exit_df = pd.DataFrame(exit_rows)
            exit_path = os.path.join(OUTPUT_PATH, f"backtest_exit_{range_str}.csv")
            exit_df.to_csv(exit_path, index=False, encoding="utf-8-sig")
            logger.info("退出分布已导出: %s", exit_path)

        return csv_path


# ------------------------------------------------------------------
# CLI 直接运行
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="选股回测引擎")
    parser.add_argument("--start", type=str, default=None, help="起始日 YYYYMMDD")
    parser.add_argument("--end", type=str, default=None, help="截止日 YYYYMMDD")
    parser.add_argument("--skip-preload", action="store_true", help="跳过预加载，使用已有缓存")
    args = parser.parse_args()

    engine = BacktestEngine(start_date=args.start, end_date=args.end)

    if not args.skip_preload:
        engine.preload_all_data()
    else:
        engine._load_from_cache()

    engine.run()
    engine.print_summary()
    engine.save_results()