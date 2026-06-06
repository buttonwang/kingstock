"""准备ML训练数据：从SQLite缓存中直接读取历史数据，提取特征并生成标签

用法:
    python scripts/prepare_training_data.py [--quick]

流程:
    1. 直接从SQLite缓存读取板块/股票日线数据
    2. 扩展为全市场前300只活跃股（不仅限于RPS候选池）
    3. 遍历每个交易日提取特征
    4. 标注多分类标签 + 排序标签 + 时间衰减权重
    5. 剔除成交额低于2000万的无效样本
    6. 按时间切片 (train/val/test) 后输出 CSV
"""
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from src.data_fetcher import DataFetcher
from src.ml.feature_engine import extract_all_features
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.utils import setup_logging
from config.settings import RPS_PERIOD, RPS_TOP_N

logger = setup_logging("prepare_training_data")

# ── 标签参数 ──
FORWARD_DAYS = 60  # 标签计算周期
LABEL_BINS = [-np.inf, -5.0, 5.0, 15.0, np.inf]  # 下跌/微涨/中涨/大涨
LABEL_NAMES = [0, 1, 2, 3]  # 0=跌(<-5%), 1=微涨(-5%~5%), 2=中涨(5%~15%), 3=大涨(>15%)
MIN_AMOUNT = 20_000_000  # 最低成交额 2000万
TOP_ACTIVE_STOCKS = 300  # 全市场前N只活跃股


def _read_cache(fetcher: DataFetcher, start_date: str, end_date: str):
    """从SQLite缓存中读取所有数据，不触发API请求"""
    from datetime import timedelta

    s_start = pd.Timestamp(start_date) - timedelta(days=200)  # 留200天用于指标计算
    s_end = pd.Timestamp(end_date)
    # 股票日线需要额外的lookahead用于计算未来收益
    s_stock_end = pd.Timestamp(end_date) + timedelta(days=FORWARD_DAYS + 30)
    fmt_start = s_start.strftime("%Y-%m-%d")
    fmt_end = s_end.strftime("%Y-%m-%d")
    fmt_stock_end = s_stock_end.strftime("%Y-%m-%d")

    # 读取板块日线缓存
    logger.info("读取板块日线缓存...")
    sector_df = fetcher._sql_to_df(
        "SELECT DISTINCT sector_name, sector_type FROM sector_daily"
    )
    sector_daily_cache = {}
    for _, row in sector_df.iterrows():
        df = fetcher._sql_to_df(
            "SELECT date, close, change_pct FROM sector_daily "
            "WHERE sector_name = ? AND sector_type = ? AND date >= ? AND date <= ? ORDER BY date",
            params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
        )
        if not df.empty:
            sector_daily_cache[row["sector_name"]] = df

    logger.info("板块日线缓存: %d 个板块", len(sector_daily_cache))

    # 读取板块成分股缓存
    logger.info("读取板块成分股缓存...")
    cons_df = fetcher._sql_to_df(
        "SELECT sector_name, sector_type, code, name FROM sector_constituents"
    )
    sector_constituents_cache = {}
    stock_name_map = {}
    for _, row in cons_df.iterrows():
        name = row["sector_name"]
        if name not in sector_constituents_cache:
            sector_constituents_cache[name] = set()
        sector_constituents_cache[name].add(row["code"])
        stock_name_map[row["code"]] = row["name"]

    logger.info("板块成分股缓存: %d 个板块, %d 只股票(去重)",
                 len(sector_constituents_cache), len(stock_name_map))

    # 读取股票日线缓存
    logger.info("读取股票日线缓存...")
    stock_daily_cache = {}
    all_codes = set()
    for codes in sector_constituents_cache.values():
        all_codes.update(codes)

    for code in all_codes:
        df = fetcher._sql_to_df(
            "SELECT date, open, high, low, close, volume, turnover_rate FROM stock_daily "
            "WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
            params=(code, fmt_start, fmt_stock_end),
        )
        if not df.empty and len(df) >= FORWARD_DAYS + 60:
            stock_daily_cache[code] = df

    logger.info("股票日线缓存: %d 只股票(>=%d行)", len(stock_daily_cache), FORWARD_DAYS)

    # 提取交易日列表
    all_dates = set()
    for df in sector_daily_cache.values():
        all_dates.update(df["date"].tolist())
    trading_dates = sorted(
        d for d in all_dates
        if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date)
    )
    logger.info("交易日: %d 天 (%s ~ %s)",
                 len(trading_dates), trading_dates[0], trading_dates[-1])

    return sector_daily_cache, sector_constituents_cache, stock_daily_cache, stock_name_map, trading_dates


def _get_top_active_stocks(stock_daily: dict, top_n: int = 300) -> set:
    """根据活跃度选出全市场前N只活跃股

    优先使用日均成交额(close*volume)，如果volume不可用则回退到:
    1. turnover_rate（换手率）
    2. 平均收盘价
    """
    stock_score = {}

    # 检查volume是否有效
    has_volume = False
    for code, df in stock_daily.items():
        if "volume" in df.columns and df["volume"].sum() > 0:
            has_volume = True
            break

    for code, df in stock_daily.items():
        if has_volume:
            # 使用成交额
            amount = df["close"].values * df["volume"].values
            amount = amount[~np.isnan(amount)]
            if len(amount) > 0:
                stock_score[code] = np.mean(amount)
        elif "turnover_rate" in df.columns and df["turnover_rate"].sum() > 0:
            # 使用换手率
            tr = df["turnover_rate"].values
            tr = tr[~np.isnan(tr)]
            if len(tr) > 0:
                stock_score[code] = np.mean(tr)
        else:
            # 使用价格（高价格通常=更活跃）
            close_vals = df["close"].values
            close_vals = close_vals[~np.isnan(close_vals)]
            if len(close_vals) > 0:
                stock_score[code] = np.mean(close_vals)

    sorted_codes = sorted(stock_score.items(), key=lambda x: -x[1])
    top_codes = {code for code, _ in sorted_codes[:top_n]}

    # 确定活跃度指标名称
    metric_name = "成交额"
    if not has_volume:
        has_turnover = any(
            "turnover_rate" in df.columns and df["turnover_rate"].sum() > 0
            for df in stock_daily.values()
        )
        metric_name = "换手率" if has_turnover else "均价"
    if sorted_codes:
        logger.info("全市场前%d只活跃股(%s): 第1名=%.2f, 第%d名=%.2f",
                    top_n, metric_name, sorted_codes[0][1],
                    min(top_n, len(sorted_codes)),
                    sorted_codes[-1][1] if len(sorted_codes) >= top_n else 0)
    return top_codes


def _get_future_return(df: pd.DataFrame, date_str: str, days: int = 60) -> float:
    """计算指定日期后的N日收益率"""
    dates = pd.to_datetime(df["date"])
    close_vals = df["close"].values
    match_idx = None
    for i, d in enumerate(dates):
        if d >= pd.Timestamp(date_str):
            match_idx = i
            break
    if match_idx is None or match_idx + days >= len(close_vals):
        return np.nan
    if close_vals[match_idx] <= 0:
        return np.nan
    return (close_vals[match_idx + days] / close_vals[match_idx] - 1) * 100


def prepare_training_data(quick: bool = False):
    """准备ML训练数据（V2增强版）"""
    fetcher = DataFetcher()

    # 使用全量可用数据
    start_date = "20251114"  # 板块数据从2025-11-14开始
    end_date = "20260603"

    logger.info("加载缓存数据 %s ~ %s...", start_date, end_date)
    sector_daily, sector_constituents, stock_daily, name_map, trading_dates = \
        _read_cache(fetcher, start_date, end_date)

    if not trading_dates:
        logger.error("无交易日数据")
        return

    fetcher.close()

    if quick and len(trading_dates) > 30:
        # 取中间段日期（跳过末尾，确保有足够前瞻数据）
        sample_size = len(trading_dates) // 5
        mid_start = len(trading_dates) // 2 - sample_size // 2
        trading_dates = trading_dates[mid_start:mid_start + sample_size]
        logger.info("快速模式: 使用 %d 个交易日 (%s ~ %s)", len(trading_dates),
                     trading_dates[0], trading_dates[-1])

    # 选出全市场前300只活跃股
    top_active = _get_top_active_stocks(stock_daily, TOP_ACTIVE_STOCKS)

    # 遍历交易日提取特征
    all_rows = []
    total_dates = len(trading_dates)
    sample_interval = max(1, total_dates // 20)

    # 预计算所有股票的60日未来收益（用于板块内排名）
    fwd_return_cache = {}

    for day_idx, date_str in enumerate(trading_dates):
        if day_idx % sample_interval == 0:
            logger.info("处理进度: %d/%d (%.0f%%)", day_idx, total_dates, 100*day_idx/total_dates)

        fmt_date = pd.Timestamp(date_str)

        # ═══ 步骤1: RPS计算（用于板块排名特征） ═══
        sector_data = {}
        for name, df in sector_daily.items():
            sub = df[pd.to_datetime(df["date"]) <= fmt_date]
            if len(sub) >= RPS_PERIOD:
                sector_data[name] = sub

        if not sector_data:
            continue

        try:
            rps_df = calculate_sector_rps(sector_data, period=RPS_PERIOD)
            top_sectors = get_top_sectors(rps_df, top_n=RPS_TOP_N)
        except Exception:
            continue

        if not rps_df.empty:
            sector_rank_map = dict(zip(rps_df["sector_name"], rps_df["rps_rank"]))
        else:
            continue

        # ═══ 步骤2: 构建候选池（全市场活跃股，非仅RPS候选） ═══
        # 找到每只股票所属板块
        code_to_sector = {}
        sector_ret_map, sector_vol_map = {}, {}

        for name, codes in sector_constituents.items():
            ret_list, vol_list = [], []
            for c in codes:
                if c in top_active:  # 只保留活跃股
                    code_to_sector[c] = name
                    sdf = stock_daily.get(c)
                    if sdf is not None:
                        m = pd.to_datetime(sdf["date"]) <= fmt_date
                        sub = sdf[m]
                        if len(sub) >= 3:
                            ret_3d = (sub["close"].values[-1] / sub["close"].values[-3] - 1) * 100
                            ret_list.append(ret_3d)
                            if "volume" in sub.columns and len(sub["volume"].dropna()) >= 3:
                                vol_list.append(sub["volume"].values[-1])
            if ret_list:
                sector_ret_map[name] = ret_list
                sector_vol_map[name] = vol_list

        # ═══ 步骤3: 对每只活跃股提取特征 ═══
        # 先收集该日所有股票的未来收益（用于板块内排名）
        daily_fwd_returns = {}  # {code: fwd_return}
        for code in top_active:
            df = stock_daily.get(code)
            if df is None:
                continue
            mask = pd.to_datetime(df["date"]) <= fmt_date
            sub = df[mask].copy()
            if len(sub) < 60:
                continue

            # 检查成交额（仅当volume数据有效时）
            if "volume" in sub.columns and "close" in sub.columns and sub["volume"].sum() > 0:
                today_vol = sub["volume"].values[-1]
                today_close = sub["close"].values[-1]
                if not np.isnan(today_vol) and not np.isnan(today_close):
                    amount = today_close * today_vol
                    if amount < MIN_AMOUNT:  # 成交额低于2000万，跳过
                        continue

            # 计算60日未来收益
            fwd_ret = _get_future_return(df, date_str, FORWARD_DAYS)
            if np.isnan(fwd_ret):
                continue
            daily_fwd_returns[code] = fwd_ret

        if not daily_fwd_returns:
            continue

        # 计算板块内排名
        sector_fwd_returns = {}  # {sector: [(code, fwd_ret), ...]}
        for code, fwd_ret in daily_fwd_returns.items():
            sector = code_to_sector.get(code)
            if sector is None:
                continue
            if sector not in sector_fwd_returns:
                sector_fwd_returns[sector] = []
            sector_fwd_returns[sector].append((code, fwd_ret))

        # 计算每只股票在板块内的百分位排名
        code_sector_rank = {}  # {code: percentile_rank (0~1, 越高越好)}
        for sector, code_list in sector_fwd_returns.items():
            if len(code_list) < 2:
                for c, _ in code_list:
                    code_sector_rank[c] = 0.5  # 板块内只有一只，给中位
                continue
            sorted_by_ret = sorted(code_list, key=lambda x: -x[1])  # 按收益降序
            total = len(sorted_by_ret)
            for rank, (c, _) in enumerate(sorted_by_ret):
                code_sector_rank[c] = 1.0 - rank / total  # 第1名=1.0, 最后=0.0

        # 提取特征
        for code, fwd_ret in daily_fwd_returns.items():
            df = stock_daily.get(code)
            if df is None:
                continue
            mask = pd.to_datetime(df["date"]) <= fmt_date
            sub = df[mask].copy()
            if len(sub) < 60:
                continue

            sector = code_to_sector.get(code, "未知")
            rps_rank = sector_rank_map.get(sector, RPS_TOP_N)

            try:
                features = extract_all_features(
                    sub, sector_returns=sector_ret_map.get(sector),
                    sector_volumes=sector_vol_map.get(sector),
                    rps_rank=rps_rank, rps_top_n=RPS_TOP_N,
                )
            except Exception:
                continue

            # ── 标签系统 ──
            # 1. 多分类标签
            label_multi = int(np.digitize(fwd_ret, LABEL_BINS[1:-1]))  # 0/1/2/3

            # 2. 二分类标签（涨/跌）
            label_cls = 1 if fwd_ret > 0 else 0

            # 3. 排序标签（板块内百分位排名）
            label_rank = code_sector_rank.get(code, 0.5)

            # 4. 时间衰减权重（越近越高）
            days_ago = (pd.Timestamp(trading_dates[-1]) - pd.Timestamp(date_str)).days
            sample_weight = np.exp(-days_ago / 180.0)  # 半衰期~180天

            record = {
                "date": date_str, "code": code, "name": name_map.get(code, ""),
                "sector": sector,
                "label_cls": label_cls,
                "label_multi": label_multi,
                "label_rank": round(label_rank, 4),
                "label_reg": round(fwd_ret, 4),
                "sample_weight": round(sample_weight, 6),
                "fwd_return": round(fwd_ret, 4),
            }
            record.update(features)
            all_rows.append(record)

    if not all_rows:
        logger.error("未生成任何训练数据")
        return

    # 构建DataFrame和时间切片
    result_df = pd.DataFrame(all_rows)
    result_df["date"] = pd.to_datetime(result_df["date"])
    result_df = result_df.sort_values("date").reset_index(drop=True)
    result_df = result_df.dropna(axis=1, how="all")

    total_rows = len(result_df)
    train_end = int(total_rows * 0.7)
    val_end = int(total_rows * 0.85)
    result_df["split"] = ""
    result_df.loc[:train_end, "split"] = "train"
    result_df.loc[train_end:val_end, "split"] = "val"
    result_df.loc[val_end:, "split"] = "test"

    feat_cols = [c for c in result_df.columns if c not in
                 ["date", "code", "name", "sector", "split",
                  "label_cls", "label_multi", "label_rank", "label_reg",
                  "sample_weight", "fwd_return"]]
    logger.info("总样本: %d | 特征维度: %d", total_rows, len(feat_cols))
    for s in ["train", "val", "test"]:
        sub = result_df[result_df["split"] == s]
        if not sub.empty:
            pos_rate = sub["label_cls"].mean() * 100
            multi_dist = sub["label_multi"].value_counts(normalize=True).sort_index()
            dist_str = ", ".join([f"{k}: {v*100:.0f}%" for k, v in multi_dist.items()])
            logger.info("  %s: %d 条, 正样本率 %.1f%%, 多分类分布 [%s]",
                        s, len(sub), pos_rate, dist_str)
        else:
            logger.info("  %s: 0 条", s)

    csv_path = os.path.join(BASE_DIR, "data", "output", "ml_training_data.csv")
    result_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    logger.info("训练数据已保存: %s (%d 条)", csv_path, total_rows)

    # 输出标签分布摘要
    print("\n" + "=" * 60)
    print("  训练数据标签分布")
    print("=" * 60)
    for label_name, cond in [
        ("下跌 (fwd<-5%)", result_df["label_multi"] == 0),
        ("微涨 (-5%~5%)", result_df["label_multi"] == 1),
        ("中涨 (5%~15%)", result_df["label_multi"] == 2),
        ("大涨 (>15%)",   result_df["label_multi"] == 3),
    ]:
        n = cond.sum()
        print(f"  {label_name:<20}: {n:>5} 条 ({n/len(result_df)*100:.1f}%)")
    print(f"  {'正样本 (fwd>0%)':<20}: {(result_df['label_cls']==1).sum():>5} 条 ({(result_df['label_cls']==1).mean()*100:.1f}%)")
    print(f"  {'时间范围':<20}: {result_df['date'].min().strftime('%Y-%m-%d')} ~ {result_df['date'].max().strftime('%Y-%m-%d')}")
    print(f"  {'特征维度':<20}: {len(feat_cols)}")
    print("=" * 60)

    return csv_path


if __name__ == "__main__":
    quick = "--quick" in sys.argv
    prepare_training_data(quick=quick)
