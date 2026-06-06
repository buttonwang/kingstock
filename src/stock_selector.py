"""主选股引擎 - 串联所有筛选器的漏斗式选股"""
import pandas as pd

from src.data_fetcher import DataFetcher
from src.filters import (
    filter_by_sector_rps,
    filter_by_macd,
    filter_by_zjtj,
    filter_by_kdj,
)
from src.indicators import calculate_macd, calculate_kdj, calculate_zjtj
from config.settings import (
    PROFIT_GROWTH_YEARS,
    PROFIT_GROWTH_MIN,
    MIN_TOTAL_SCORE,
    MAX_DAILY_OUTPUT,
    ENHANCED_RULES_MIN,
)
from src.utils import setup_logging, get_trade_date
from src.manual_analyzer import analyze_manual_stocks
from src.ebk_analyzer import analyze_ebk_stocks
from src.scoring import compute_total_score
from src.indicators.enhanced_rules import check_all_enhanced_rules

logger = setup_logging("stock_selector")


class StockSelector:
    """选股引擎 - 并行筛选（MACD∩ZJTJ为核心，KDJ/财务为加分项）"""

    def __init__(self, force_update: bool = False):
        """初始化DataFetcher和logger

        参数:
            force_update: 是否强制重新拉取数据（忽略日线缓存）
        """
        self.fetcher = DataFetcher()
        self.force_update = force_update
        if force_update:
            self._clear_daily_cache()
        self.stage_codes = {}  # 各阶段筛选出的股票代码集合
        self.stage_descriptions = {
            "1_RPS板块": "板块RPS排名前20板块成分股",
            "2_MACD": "MACD买入信号",
            "3_ZJTJ": "ZJTJ庄家控盘",
            "4_KDJ": "KDJ买入信号(加分)",
            "5_财务": "财务基本面筛选(加分)",
        }
        self._bonus_kdj_codes = set()
        self._bonus_finance_codes = set()
        self.manual_result = None  # 手工选股分析结果
        self.ebk_result = None  # 龙头公司EBK分析结果

    def _clear_daily_cache(self):
        """清空日线相关的缓存表，强制重新拉取数据"""
        try:
            with self.fetcher._cursor() as cur:
                cur.execute("DELETE FROM stock_daily")
                cur.execute("DELETE FROM sector_daily")
            logger.info("已清空日线缓存（force_update模式）")
        except Exception as e:
            logger.warning("清空缓存失败: %s", e)

    def run(self, date: str = None) -> pd.DataFrame:
        """执行完整选股流程

        参数:
            date - 选股日期(YYYYMMDD格式)，默认当天

        流程：
        1. 确定日期范围（需要至少60个交易日的历史数据用于指标计算）
        2. 规则1：板块RPS筛选 - 调用filter_by_sector_rps获取热门板块股票池
        3. 准备数据：为规则1筛选出的股票批量获取日线数据（构建stock_daily_dict）
        4. 规则2-5：并行筛选 - MACD、ZJTJ、KDJ、财务均基于规则1股票池独立筛选
        5. 综合结果：MACD ∩ ZJTJ 作为核心条件（必须同时满足），KDJ和财务作为加分项

        每一步打印日志：
        "[规则X] XXX筛选: 输入N只 → 输出M只"

        返回: DataFrame[code, name, sector, dif, dea, macd, k, d, j, kongpan,
                        kdj_pass, finance_pass, bonus_count]
        """
        # ---- 1. 确定日期范围 ----
        end_date = get_trade_date(date)
        # 需要60+个交易日历史数据，约120个自然日；取200天余量
        start_date = (
            pd.Timestamp(end_date) - pd.Timedelta(days=200)
        ).strftime("%Y%m%d")

        logger.info("=" * 60)
        logger.info("开始选股流程，选股日期: %s", end_date)
        logger.info("历史数据范围: %s ~ %s", start_date, end_date)
        logger.info("=" * 60)

        # 获取股票列表（用于名称映射和输入计数）
        all_stocks = self.fetcher.get_all_stocks()
        stock_name_map = dict(zip(all_stocks["code"], all_stocks["name"]))
        total_input = len(all_stocks)

        # ---- 2. 规则1：板块RPS筛选 ----
        sector_codes = filter_by_sector_rps(
            self.fetcher, start_date, end_date
        )
        self.stage_codes["1_RPS板块"] = sector_codes
        logger.info(
            "[规则1] 板块RPS筛选: 输入%d只 → 输出%d只",
            total_input, len(sector_codes),
        )

        if not sector_codes:
            logger.info("规则1筛选后无股票，选股结束")
            return pd.DataFrame(
                columns=["code", "name", "sector", "dif", "dea", "macd",
                         "k", "d", "j", "kongpan"]
            )

        # ---- 3. 准备数据：只为候选股票获取日线 ----
        logger.info("开始获取 %d 只候选股票的日线数据...", len(sector_codes))
        stock_daily_dict = {}
        codes_list = sorted(sector_codes)
        for i, code in enumerate(codes_list, 1):
            try:
                df = self.fetcher.get_stock_daily(code, start_date, end_date)
                if not df.empty:
                    stock_daily_dict[code] = df
            except Exception as e:
                logger.warning("获取 %s 日线数据失败: %s", code, e)

            if i % 100 == 0 or i == len(codes_list):
                logger.info("日线数据获取进度: %d/%d (已获取 %d 只)",
                            i, len(codes_list), len(stock_daily_dict))

        logger.info("成功获取 %d 只股票的日线数据", len(stock_daily_dict))

        if not stock_daily_dict:
            logger.info("无可用日线数据，选股结束")
            return pd.DataFrame(
                columns=["code", "name", "sector", "dif", "dea", "macd",
                         "k", "d", "j", "kongpan"]
            )

        # ---- 4. 规则2、3、4、5：并行筛选（均基于规则1股票池） ----
        pool_size = len(stock_daily_dict)

        # 规则2：MACD买入信号
        macd_codes = filter_by_macd(stock_daily_dict)
        self.stage_codes["2_MACD"] = macd_codes
        logger.info(
            "[规则2] MACD买入信号筛选: 输入%d只 → 输出%d只",
            pool_size, len(macd_codes),
        )

        # 规则3：ZJTJ庄家控盘
        zjtj_codes = filter_by_zjtj(stock_daily_dict)
        self.stage_codes["3_ZJTJ"] = zjtj_codes
        logger.info(
            "[规则3] ZJTJ庄家控盘筛选: 输入%d只 → 输出%d只",
            pool_size, len(zjtj_codes),
        )

        # 规则4：KDJ买入信号
        kdj_codes = filter_by_kdj(stock_daily_dict)
        self.stage_codes["4_KDJ"] = kdj_codes
        logger.info(
            "[规则4] KDJ买入信号筛选: 输入%d只 → 输出%d只",
            pool_size, len(kdj_codes),
        )

        # 规则5：财务基本面筛选
        finance_codes = self._filter_finance_for_candidates(
            stock_daily_dict.keys()
        )
        self.stage_codes["5_财务"] = finance_codes
        logger.info(
            "[规则5] 财务基本面筛选: 输入%d只 → 输出%d只",
            pool_size, len(finance_codes),
        )

        # ---- 5. 综合结果：MACD ∩ ZJTJ 为核心，KDJ和财务为加分项 ----
        core_codes = macd_codes & zjtj_codes
        self.stage_codes["全部规则"] = core_codes
        self._bonus_kdj_codes = kdj_codes
        self._bonus_finance_codes = finance_codes

        final_result_df = self._build_result(
            {k: v for k, v in stock_daily_dict.items() if k in core_codes},
            stock_name_map,
        )

        # V2: 评分过滤 + 最大输出数限制
        if not final_result_df.empty and "total_score" in final_result_df.columns:
            before_count = len(final_result_df)
            # 按总分降序排列
            final_result_df = final_result_df.sort_values(
                "total_score", ascending=False
            ).reset_index(drop=True)

            # 最低评分过滤
            final_result_df = final_result_df[
                final_result_df["total_score"] >= MIN_TOTAL_SCORE
            ]

            # 每日最多输出限制
            if len(final_result_df) > MAX_DAILY_OUTPUT:
                final_result_df = final_result_df.head(MAX_DAILY_OUTPUT)

            logger.info(
                "V2评分过滤: %d只 → %d只 (最低分%d, 最多%d只)",
                before_count, len(final_result_df),
                MIN_TOTAL_SCORE, MAX_DAILY_OUTPUT,
            )

        if not final_result_df.empty:
            # 统计加分项分布
            b2 = (final_result_df["加分合计"] == 2).sum()
            b1 = (final_result_df["加分合计"] == 1).sum()
            b0 = (final_result_df["加分合计"] == 0).sum()
            logger.info(
                "选股完成，核心(MACD∩ZJTJ): %d只 | 加分2项:%d 加分1项:%d 加分0项:%d",
                len(final_result_df), b2, b1, b0,
            )
        else:
            logger.info("核心条件(MACD∩ZJTJ)筛选后无股票")

        # ---- 6. 手工选股分析 ----
        try:
            logger.info("开始手工选股分析...")
            self.manual_result = analyze_manual_stocks(
                self.fetcher, start_date, end_date, stock_name_map
            )
            if not self.manual_result.empty:
                core_manual = self.manual_result[self.manual_result["core_pass"]]
                logger.info(
                    "手工选股: %d只中 %d只满足核心条件(MACD∩ZJTJ)",
                    len(self.manual_result), len(core_manual),
                )
        except Exception as e:
            logger.warning("手工选股分析异常: %s", e)
            self.manual_result = None

        # ---- 7. 龙头公司EBK分析 ----
        try:
            logger.info("开始龙头公司EBK分析...")
            # 不排除主选股结果，以便找出King Stock
            self.ebk_result = analyze_ebk_stocks(
                self.fetcher, start_date, end_date, stock_name_map,
                exclude_codes=None,
            )
            if not self.ebk_result.empty:
                core_ebk = self.ebk_result[self.ebk_result["core_pass"]]
                logger.info(
                    "龙头公司EBK: %d只中 %d只满足核心条件(MACD∩ZJTJ)",
                    len(self.ebk_result), len(core_ebk),
                )
        except Exception as e:
            logger.warning("龙头公司EBK分析异常: %s", e)
            self.ebk_result = None

        return final_result_df

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _filter_finance_for_candidates(self, candidate_codes) -> set:
        """对候选股票进行财务基本面筛选（只检查候选股票，提高效率）

        逻辑与 filter_by_finance 一致：
        - 近PROFIT_GROWTH_YEARS+1年净利润均为正
        - 每年同比增长
        - 复合年化增长率 >= PROFIT_GROWTH_MIN
        """
        result = set()
        codes_list = list(candidate_codes)
        total = len(codes_list)

        for i, code in enumerate(codes_list, 1):
            try:
                profit_df = self.fetcher.get_profit_data(code)
                if profit_df.empty:
                    continue

                # 复制并处理
                profit_df = profit_df.copy()
                profit_df["year"] = pd.to_datetime(
                    profit_df["report_date"]
                ).dt.year
                profit_df = profit_df.sort_values("year")

                # 需要N+1年数据来算N年增长
                needed_years = PROFIT_GROWTH_YEARS + 1
                recent = profit_df.tail(needed_years)
                if len(recent) < needed_years:
                    continue

                profits = recent["net_profit"].values

                # 条件1: 净利润均为正
                if any(p <= 0 for p in profits):
                    continue

                # 条件2: 每年同比增长
                yoy_growth = True
                for j in range(1, len(profits)):
                    if profits[j] <= profits[j - 1]:
                        yoy_growth = False
                        break
                if not yoy_growth:
                    continue

                # 条件3: 复合年化增长率 >= 阈值
                earliest = profits[0]
                latest = profits[-1]
                n_years = len(profits) - 1
                if earliest <= 0:
                    continue
                cagr = (latest / earliest) ** (1.0 / n_years) - 1
                if cagr >= PROFIT_GROWTH_MIN:
                    result.add(code)

            except Exception as e:
                logger.warning("股票 %s 财务筛选失败: %s", code, e)
                continue

            if total <= 20 or i % 10 == 0 or i == total:
                logger.info("财务筛选进度: %d/%d，当前命中 %d 只",
                            i, total, len(result))

        logger.info("财务筛选完成: %d 只中 %d 只满足条件", total, len(result))
        return result

    def _get_stock_sector(self, code: str) -> str:
        """从数据库缓存中获取股票所属板块名称"""
        try:
            df = self.fetcher._sql_to_df(
                "SELECT sector_name FROM sector_constituents "
                "WHERE code = ? LIMIT 1",
                params=(code,),
            )
            if not df.empty:
                return df.iloc[0]["sector_name"]
        except Exception:
            pass
        return ""

    def build_stage_details(self, stock_name_map: dict) -> dict:
        """构建各阶段筛选明细，用于输出到Excel多个Sheet

        返回: { sheet_name: DataFrame }
        """
        sheets = {}

        for stage_key in ["1_RPS板块", "2_MACD", "3_ZJTJ", "4_KDJ", "5_财务"]:
            codes = self.stage_codes.get(stage_key, set())
            desc = self.stage_descriptions.get(stage_key, "")

            if not codes:
                df = pd.DataFrame(columns=["code", "name"])
            else:
                rows = []
                for code in sorted(codes):
                    sector = self._get_stock_sector(code)
                    rows.append({
                        "code": code,
                        "name": stock_name_map.get(code, ""),
                        "sector": sector,
                    })
                df = pd.DataFrame(rows)

            # 在开头加一行描述
            header = pd.DataFrame([{"code": f"【{desc}】共 {len(codes)} 只", "name": "", "sector": ""}])
            sheets[stage_key] = pd.concat([header, df], ignore_index=True)

        # 最终结果Sheet（核心 = MACD∩ZJTJ，KDJ和财务为加分项）
        final_codes = self.stage_codes.get("全部规则", set())

        if not final_codes:
            sheets["最终结果"] = pd.DataFrame(columns=["code", "name", "sector",
                                                       "dif", "dea", "macd",
                                                       "k", "d", "j", "kongpan",
                                                       "KDJ加分", "财务加分", "加分合计"])
        else:
            final_rows = []
            for code in sorted(final_codes):
                sector = self._get_stock_sector(code)
                kdj_pass = code in self._bonus_kdj_codes
                finance_pass = code in self._bonus_finance_codes
                bonus_count = sum([kdj_pass, finance_pass])
                final_rows.append({
                    "code": code,
                    "name": stock_name_map.get(code, ""),
                    "sector": sector,
                    "KDJ加分": "✓" if kdj_pass else "",
                    "财务加分": "✓" if finance_pass else "",
                    "加分合计": bonus_count,
                })
            sheets["最终结果"] = pd.DataFrame(final_rows)

        return sheets

    def _build_result(self, stock_daily_dict: dict,
                      stock_name_map: dict) -> pd.DataFrame:
        """构建最终结果DataFrame，附带各指标数值和加分项状态

        KDJ和财务为加分项（非必须），结果中体现每只股票满足几项加分条件

        V2 新增：综合评分 + 增强规则检查
        """
        rows = []
        for code, df in stock_daily_dict.items():
            try:
                # 计算各项指标，取最新一行数值
                df_macd = calculate_macd(df)
                df_kdj = calculate_kdj(df)
                df_zjtj = calculate_zjtj(df)

                latest_macd = df_macd.iloc[-1]
                latest_kdj = df_kdj.iloc[-1]
                latest_zjtj = df_zjtj.iloc[-1]

                sector = self._get_stock_sector(code)

                # 加分项判定
                kdj_pass = code in self._bonus_kdj_codes
                finance_pass = code in self._bonus_finance_codes
                bonus_count = sum([kdj_pass, finance_pass])

                # V2: 综合评分（不含RPS和财务CAGR，简化处理）
                scores = compute_total_score(
                    df_macd, df_kdj, df_zjtj,
                    rps_rank=0, cagr=0.0,
                )
                total_score = scores["total_score"]

                # V2: 增强规则检查
                enh = check_all_enhanced_rules(df)
                enh_passed = enh["rules_passed"]

                # V2: 增强规则硬过滤（启用时）
                if ENHANCED_RULES_MIN > 0 and enh_passed < ENHANCED_RULES_MIN:
                    continue

                rows.append({
                    "code": code,
                    "name": stock_name_map.get(code, ""),
                    "sector": sector,
                    "dif": round(latest_macd["dif"], 4),
                    "dea": round(latest_macd["dea"], 4),
                    "macd": round(latest_macd["macd"], 4),
                    "k": round(latest_kdj["k"], 2),
                    "d": round(latest_kdj["d"], 2),
                    "j": round(latest_kdj["j"], 2),
                    "kongpan": round(latest_zjtj["kongpan"], 2),
                    "KDJ加分": "✓" if kdj_pass else "",
                    "财务加分": "✓" if finance_pass else "",
                    "加分合计": bonus_count,
                    "total_score": total_score,
                })
            except Exception as e:
                logger.warning("构建 %s 结果行失败: %s", code, e)
                continue

        result = pd.DataFrame(
            rows,
            columns=["code", "name", "sector", "dif", "dea", "macd",
                     "k", "d", "j", "kongpan", "KDJ加分", "财务加分",
                     "加分合计", "total_score"],
        )

        # V2: 按总分降序排列
        if not result.empty:
            result = result.sort_values(
                "total_score", ascending=False
            ).reset_index(drop=True)

        return result
