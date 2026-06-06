"""60+维量价技术特征工程模块

从单只股票的OHLCV日线数据中提取六大类特征：
1. 价格动量特征 (15个)
2. 波动率特征 (8个)
3. 成交量特征 (10个)
4. 技术指标特征 (15个)
5. 横截面特征 (10个，需外部传入板块信息)
6. 综合派生特征 (5个)
"""

import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning)

# ── 需要的列 ──
_REQUIRED_COLS = {"open", "high", "low", "close"}
_OPTIONAL_COLS = {"volume"}


def validate_df(df: pd.DataFrame) -> bool:
    """验证DataFrame是否包含必要列且数据量足够"""
    if df is None or df.empty:
        return False
    if not _REQUIRED_COLS.issubset(set(df.columns)):
        return False
    if len(df) < 60:
        return False
    return True


# ===================================================================
# 1. 价格动量特征
# ===================================================================

def _price_momentum_features(df: pd.DataFrame) -> dict:
    """价格动量特征 (15个)"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    feature = {}

    # 1.1 过去N日收益率
    for n in [1, 3, 5, 10, 20, 60]:
        if len(close) > n:
            ret = (close[-1] / close[-1 - n] - 1) * 100
        else:
            ret = 0.0
        feature[f"ret_{n}d"] = round(ret, 4)

    # 1.2 价格相对位置 (close - low_N) / (high_N - low_N)
    for n in [20, 60]:
        if len(close) > n:
            h = high[-n:].max()
            l = low[-n:].min()
            pos = (close[-1] - l) / (h - l) if (h - l) > 1e-10 else 0.5
        else:
            pos = 0.5
        feature[f"price_position_{n}d"] = round(pos, 4)

    # 1.3 价格与均线比 close / SMA_N
    for n in [5, 10, 20, 60]:
        if len(close) >= n:
            sma = np.mean(close[-n:])
            ratio = close[-1] / sma if sma > 1e-10 else 1.0
        else:
            ratio = 1.0
        feature[f"close_sma_{n}d"] = round(ratio, 4)

    # 1.4 均线多头排列状态
    if len(close) >= 60:
        sma5 = np.mean(close[-5:])
        sma10 = np.mean(close[-10:])
        sma20 = np.mean(close[-20:])
        sma60 = np.mean(close[-60:])
        feature["ma_bullish"] = int(sma5 > sma10 > sma20 > sma60)
    else:
        feature["ma_bullish"] = 0

    # 散度: 短期均线偏离长期均线的程度
    if len(close) >= 60:
        feature["ma_short_long_ratio"] = round(sma5 / sma60, 4) if sma60 > 1e-10 else 1.0
    else:
        feature["ma_short_long_ratio"] = 1.0

    return feature


# ===================================================================
# 2. 波动率特征
# ===================================================================

def _volatility_features(df: pd.DataFrame) -> dict:
    """波动率特征 (8个)"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    feature = {}

    # 2.1 收益率标准差 (年化波动率近似)
    returns = np.diff(np.log(close))
    for n in [5, 10, 20, 60]:
        if len(returns) >= n:
            vol = np.std(returns[-n:]) * np.sqrt(252)
        else:
            vol = 0.0
        feature[f"volatility_{n}d"] = round(vol, 4)

    # 2.2 ATR(14) 归一化
    if len(close) >= 15:
        tr = np.maximum(
            high[1:] - low[1:],
            np.maximum(
                np.abs(high[1:] - close[:-1]),
                np.abs(low[1:] - close[:-1]),
            ),
        )
        atr = np.mean(tr[-14:])
        feature["atr_14"] = round(atr / close[-1], 4) if close[-1] > 1e-10 else 0.0
    else:
        feature["atr_14"] = 0.0

    # 2.3 最大回撤 (近20/60日)
    for n in [20, 60]:
        if len(close) >= n:
            peak = np.maximum.accumulate(close[-n:])
            drawdown = (close[-n:] - peak) / peak
            feature[f"max_drawdown_{n}d"] = round(float(np.min(drawdown)), 4)
        else:
            feature[f"max_drawdown_{n}d"] = 0.0

    return feature


# ===================================================================
# 3. 成交量特征
# ===================================================================

def _volume_features(df: pd.DataFrame) -> dict:
    """成交量特征 (10个)"""
    if "volume" not in df.columns:
        # 如果没有成交量数据，返回默认值
        return {
            "vol_ratio_5d": 1.0, "vol_ratio_20d": 1.0,
            "vol_ratio_change": 0.0, "obv_trend": 0.0,
            "vol_price_corr_5d": 0.0, "vol_price_corr_10d": 0.0,
            "vol_turnover_rate": 0.0, "vol_std_20d": 0.0,
            "vol_rank": 0.5, "vol_trend": 0.0,
        }

    close = df["close"].values
    volume = df["volume"].values
    feature = {}

    # 过滤异常成交量
    vol_valid = np.where(volume > 0, volume, np.nan)
    vol_safe = np.nan_to_num(vol_valid, nan=1.0)

    # 3.1 成交量相对均量
    for n in [5, 20]:
        if len(vol_safe) >= n:
            avg_vol = np.nanmean(vol_safe[-n:])
            ratio = vol_safe[-1] / avg_vol if avg_vol > 1e-10 else 1.0
        else:
            ratio = 1.0
        feature[f"vol_ratio_{n}d"] = round(ratio, 4)

    # 3.2 量比变化趋势
    if len(vol_safe) >= 20:
        r5 = np.nanmean(vol_safe[-5:]) / max(np.nanmean(vol_safe[-20:-5]), 1e-10)
        feature["vol_ratio_change"] = round(r5, 4)
    else:
        feature["vol_ratio_change"] = 1.0

    # 3.3 OBV 趋势 (过去10日OBV斜率)
    if len(close) >= 11 and len(vol_safe) >= 11:
        obv = [0.0]
        for i in range(1, len(close[-20:])):
            if close[-20:][i] > close[-20:][i-1]:
                obv.append(obv[-1] + vol_safe[-20:][i])
            elif close[-20:][i] < close[-20:][i-1]:
                obv.append(obv[-1] - vol_safe[-20:][i])
            else:
                obv.append(obv[-1])
        if len(obv) >= 2:
            obv_slope = (obv[-1] - obv[0]) / max(obv[-1], 1e-10)
            feature["obv_trend"] = round(float(obv_slope), 4)
        else:
            feature["obv_trend"] = 0.0
    else:
        feature["obv_trend"] = 0.0

    # 3.4 量价相关性
    for n in [5, 10]:
        if len(close) >= n + 1 and len(vol_safe) >= n + 1:
            c_ret = np.diff(np.log(close[-(n+1):]))
            v_ret = np.diff(np.log(vol_safe[-(n+1):]))
            if len(c_ret) > 1 and np.std(c_ret) > 1e-10 and np.std(v_ret) > 1e-10:
                corr = np.corrcoef(c_ret, v_ret)[0, 1]
            else:
                corr = 0.0
        else:
            corr = 0.0
        feature[f"vol_price_corr_{n}d"] = round(float(corr), 4)

    # 3.5 换手率 (如果存在)
    if "turnover_rate" in df.columns:
        tr = df["turnover_rate"].values
        tr_v = np.where(pd.notna(tr), tr, 0)
        feature["vol_turnover_rate"] = round(float(tr_v[-1]), 4) if len(tr_v) > 0 else 0.0
    else:
        feature["vol_turnover_rate"] = 0.0

    return feature


# ===================================================================
# 4. 技术指标特征
# ===================================================================

def _rsi(close: np.ndarray, n: int) -> float:
    """计算 RSI"""
    if len(close) < n + 1:
        return 50.0
    deltas = np.diff(close[-(n+1):])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger(close: np.ndarray, n: int = 20, k: float = 2.0) -> tuple:
    """布林带: (%B, 带宽)"""
    if len(close) < n:
        return 0.5, 0.0
    sma = np.mean(close[-n:])
    std = np.std(close[-n:])
    upper = sma + k * std
    lower = sma - k * std
    if upper - lower < 1e-10:
        return 0.5, 0.0
    pct_b = (close[-1] - lower) / (upper - lower)
    bandwidth = (upper - lower) / sma
    return round(float(pct_b), 4), round(float(bandwidth), 4)


def _williams_r(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = 14) -> float:
    """威廉指标 WR"""
    if len(close) < n:
        return -50.0
    h = high[-n:].max()
    l = low[-n:].min()
    if h - l < 1e-10:
        return -50.0
    return round(float((h - close[-1]) / (h - l) * (-100.0)), 2)


def _technical_indicator_features(df: pd.DataFrame) -> dict:
    """技术指标特征 (15+个)"""
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    feature = {}

    # 4.1 MACD 特征
    if len(close) >= 26:
        ema12 = pd.Series(close).ewm(span=12, adjust=False).mean().values
        ema26 = pd.Series(close).ewm(span=26, adjust=False).mean().values
        dif = ema12 - ema26
        dea = pd.Series(dif).ewm(span=9, adjust=False).mean().values
        macd_bar = 2 * (dif - dea)

        feature["macd_dif"] = round(float(dif[-1]), 4)
        feature["macd_dea"] = round(float(dea[-1]), 4)
        feature["macd_bar"] = round(float(macd_bar[-1]), 4)
        feature["macd_dif_dea_gap"] = round(float(dif[-1] - dea[-1]), 4)
        # MACD方向: 当前macd_bar > 0?
        feature["macd_positive"] = int(macd_bar[-1] > 0)
        # MACD金叉死叉状态
        if len(macd_bar) >= 2:
            feature["macd_cross_up"] = int(dif[-1] > dea[-1] and dif[-2] <= dea[-2])
            feature["macd_cross_down"] = int(dif[-1] < dea[-1] and dif[-2] >= dea[-2])
        else:
            feature["macd_cross_up"] = 0
            feature["macd_cross_down"] = 0
    else:
        for k in ["macd_dif", "macd_dea", "macd_bar", "macd_dif_dea_gap"]:
            feature[k] = 0.0
        feature["macd_positive"] = 0
        feature["macd_cross_up"] = 0
        feature["macd_cross_down"] = 0

    # 4.2 KDJ 特征
    if len(close) >= 9:
        low_n = pd.Series(low).rolling(9).min().values
        high_n = pd.Series(high).rolling(9).max().values
        rsv = np.where(
            high_n - low_n != 0,
            (close - low_n) / (high_n - low_n) * 100,
            50,
        )
        k_val = pd.Series(rsv).ewm(alpha=1/3, adjust=False).mean().values
        d_val = pd.Series(k_val).ewm(alpha=1/3, adjust=False).mean().values
        j_val = 3 * k_val - 2 * d_val

        feature["kdj_k"] = round(float(k_val[-1]), 2)
        feature["kdj_d"] = round(float(d_val[-1]), 2)
        feature["kdj_j"] = round(float(j_val[-1]), 2)
        feature["kdj_golden_cross"] = int(k_val[-1] > d_val[-1] and k_val[-2] <= d_val[-2]) if len(k_val) >= 2 else 0
        feature["kdj_j_direction"] = int(j_val[-1] > j_val[-2]) if len(j_val) >= 2 else 0
    else:
        for k in ["kdj_k", "kdj_d", "kdj_j"]:
            feature[k] = 50.0
        feature["kdj_golden_cross"] = 0
        feature["kdj_j_direction"] = 0

    # 4.3 ZJTJ 特征 (使用已有计算)
    # 控盘度：用价格强度近似
    if len(close) >= 20:
        strength = (close[-1] / np.mean(close[-20:]) - 1) * 100
        feature["zjtj_price_strength"] = round(float(strength), 4)
    else:
        feature["zjtj_price_strength"] = 0.0

    # 4.4 RSI
    for n in [6, 12, 24]:
        feature[f"rsi_{n}"] = round(_rsi(close, n), 2)

    # 4.5 布林带
    pct_b, bw = _bollinger(close)
    feature["boll_pct_b"] = pct_b
    feature["boll_bandwidth"] = bw

    # 4.6 威廉指标
    feature["wr_10"] = _williams_r(high, low, close, 10)
    feature["wr_20"] = _williams_r(high, low, close, 20)

    return feature


# ===================================================================
# 5. 横截面特征 (需外部传入板块信息)
# ===================================================================


def _cross_section_features(df: pd.DataFrame, sector_returns: list = None,
                             sector_volumes: list = None,
                             rps_rank: int = None, rps_top_n: int = 20,
                             sector_corr: float = None) -> dict:
    """横截面特征

    参数:
        sector_returns: 同板块其他股票的同期收益率列表
        sector_volumes: 同板块其他股票的量比列表
        rps_rank: 板块RPS排名
        rps_top_n: RPS排名总数
    """
    feature = {}

    # 收益率在板块内的百分位
    if sector_returns and len(sector_returns) > 0:
        my_ret = (df["close"].values[-1] / df["close"].values[-3] - 1) * 100
        combined = list(sector_returns) + [my_ret]
        rank = sum(1 for v in combined if v >= my_ret)
        feature["ret_rank_in_sector"] = round(rank / len(combined), 4)
    else:
        feature["ret_rank_in_sector"] = 0.5

    # 成交量在板块内的百分位
    if sector_volumes and len(sector_volumes) > 0 and "volume" in df.columns:
        vol = df["volume"].values[-1]
        combined_v = list(sector_volumes) + [vol]
        rank_v = sum(1 for v in combined_v if v >= vol)
        feature["vol_rank_in_sector"] = round(rank_v / len(combined_v), 4)
    else:
        feature["vol_rank_in_sector"] = 0.5

    # RPS排名
    if rps_rank is not None and rps_top_n > 0:
        feature["rps_normalized"] = round(1.0 - (rps_rank - 1) / rps_top_n, 4)
    else:
        feature["rps_normalized"] = 0.5

    # V2: 板块联动特征
    if sector_corr is not None:
        feature["sector_corr"] = round(float(sector_corr), 4)
    else:
        feature["sector_corr"] = 0.0

    return feature


# ===================================================================
# 6. 综合派生特征
# ===================================================================

def _derived_features(df: pd.DataFrame) -> dict:
    """综合派生特征 (5个 V2 增强版)

    V2 新增：量价背离、波动率比、资金流向代理
    """
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    feature = {}

    # 6.1 价格加速度: 收益率的变化率
    if len(close) >= 21:
        ret_5 = close[-1] / close[-6] - 1
        ret_20 = close[-6] / close[-21] - 1
        feature["price_acceleration"] = round(float(ret_5 - ret_20), 4)
    else:
        feature["price_acceleration"] = 0.0

    # 6.2 日均振幅
    if len(close) >= 20:
        amplitude = (high[-20:] - low[-20:]) / close[-20:]
        feature["avg_amplitude_20d"] = round(float(np.mean(amplitude)), 4)
    else:
        feature["avg_amplitude_20d"] = 0.0

    # 6.3 近期创新高/新低
    if len(close) >= 60:
        feature["near_60d_high"] = int(close[-1] >= high[-60:].max() * 0.97)
        feature["near_60d_low"] = int(close[-1] <= low[-60:].min() * 1.03)
    else:
        feature["near_60d_high"] = 0
        feature["near_60d_low"] = 0

    # 6.4 价格线性斜率 (近20日)
    if len(close) >= 20:
        x = np.arange(20)
        y = close[-20:]
        if np.std(y) > 1e-10:
            slope = np.polyfit(x, y, 1)[0]
            feature["price_slope_20d"] = round(float(slope / np.mean(y) * 100), 4)
        else:
            feature["price_slope_20d"] = 0.0
    else:
        feature["price_slope_20d"] = 0.0

    # ============================================================
    # V2 新增特征
    # ============================================================

    # 6.5 量价背离特征: 价格创新低但成交量萎缩比例
    if len(close) >= 20 and "volume" in df.columns:
        vol = df["volume"].values
        vol_safe = np.where(np.isnan(vol) | (vol <= 0), 1, vol)

        # 计算近20日最低价
        recent_low_20 = low[-20:]
        recent_vol_20 = vol_safe[-20:]

        # 检查最低价是否出现在最近5日
        min_idx_20 = np.argmin(recent_low_20)
        min_is_recent = int(min_idx_20 >= len(recent_low_20) - 5)

        # 如果最低价在近5日，检查成交量是否萎缩
        if min_is_recent:
            vol_at_low = recent_vol_20[min_idx_20]
            avg_vol_except_low = np.mean(np.delete(recent_vol_20, min_idx_20))
            vol_shrink_ratio = vol_at_low / avg_vol_except_low if avg_vol_except_low > 1e-10 else 1.0
        else:
            vol_shrink_ratio = 1.0

        feature["price_low_vol_shrink"] = round(float(vol_shrink_ratio), 4)
        feature["price_low_is_recent"] = min_is_recent
    else:
        feature["price_low_vol_shrink"] = 1.0
        feature["price_low_is_recent"] = 0

    # 6.6 波动率比: 近20日/60日波动率
    if len(close) >= 60:
        returns_all = np.diff(np.log(close))
        vol_20 = np.std(returns_all[-20:]) if len(returns_all) >= 20 else 0
        vol_60 = np.std(returns_all[-60:]) if len(returns_all) >= 60 else 0
        vol_ratio = vol_20 / vol_60 if vol_60 > 1e-10 else 1.0
        feature["vol_ratio_20_60"] = round(float(vol_ratio), 4)

        # 短期波动趋势: 近5日波动 / 近20日波动
        vol_5 = np.std(returns_all[-5:]) if len(returns_all) >= 5 else 0
        vol_short_ratio = vol_5 / vol_20 if vol_20 > 1e-10 else 1.0
        feature["vol_ratio_5_20"] = round(float(vol_short_ratio), 4)
    else:
        feature["vol_ratio_20_60"] = 1.0
        feature["vol_ratio_5_20"] = 1.0

    # 6.7 资金流向代理: Volume Accumulation/Distribution
    if len(close) >= 20 and "volume" in df.columns:
        vol = df["volume"].values
        vol_safe = np.where(np.isnan(vol) | (vol <= 0), 1, vol)

        # 上涨日成交量占比 (近20日)
        up_vol_sum = 0.0
        total_vol_sum = 0.0
        for i in range(-20, 0):
            if abs(i) >= len(close):
                continue
            idx = i
            total_vol_sum += vol_safe[idx]
            if idx > 0 and close[idx] > close[idx - 1]:
                up_vol_sum += vol_safe[idx]
            elif idx == 0:
                # 今天就算上涨也部分计入
                up_vol_sum += vol_safe[idx] * 0.5

        up_vol_ratio = up_vol_sum / total_vol_sum if total_vol_sum > 0 else 0.5
        feature["up_vol_ratio_20d"] = round(float(up_vol_ratio), 4)

        # ADL指标简化版: 累积 (收盘价位置 * 成交量)
        adl = np.zeros(len(close))
        for i in range(1, len(close)):
            hl = high[i] - low[i]
            if hl > 1e-10:
                clv = ((close[i] - low[i]) - (high[i] - close[i])) / hl
            else:
                clv = 0
            adl[i] = adl[i-1] + clv * vol_safe[i]

        # 近20日ADL斜率
        if len(adl) >= 20:
            adl_recent = adl[-20:]
            adl_range = adl_recent[-1] - adl_recent[0]
            adl_slope = adl_range / (abs(adl_recent[0]) + 1e-10)
            feature["adl_slope_20d"] = round(float(adl_slope), 4)
            feature["adl_trend"] = int(adl_range > 0)
        else:
            feature["adl_slope_20d"] = 0.0
            feature["adl_trend"] = 0
    else:
        feature["up_vol_ratio_20d"] = 0.5
        feature["adl_slope_20d"] = 0.0
        feature["adl_trend"] = 0

    return feature


# ===================================================================
# 主接口
# ===================================================================

def extract_all_features(df: pd.DataFrame,
                          sector_returns: list = None,
                          sector_volumes: list = None,
                          rps_rank: int = None,
                          rps_top_n: int = 20,
                          sector_corr: float = None) -> dict:
    """从单只股票的OHLCV日线数据中提取全部80+特征

    参数:
        df: DataFrame，需包含[date, open, high, low, close, volume](volume可选)
        sector_returns: 同板块其他股票近期收益列表(用于横截面排名)
        sector_volumes: 同板块其他股票成交量列表(用于横截面排名)
        rps_rank: 所属板块的RPS排名
        rps_top_n: RPS TOP N 参数

    返回:
        特征字典 (80+ key)
    """
    if not validate_df(df):
        raise ValueError("DataFrame无效或数据量不足(需>=60行)")

    features = {}
    features.update(_price_momentum_features(df))
    features.update(_volatility_features(df))
    features.update(_volume_features(df))
    features.update(_technical_indicator_features(df))
    features.update(_cross_section_features(df, sector_returns, sector_volumes,
                                             rps_rank, rps_top_n, sector_corr))
    features.update(_derived_features(df))

    return features


def extract_features_series(df: pd.DataFrame, **kwargs) -> pd.Series:
    """返回pd.Series格式的特征（用于模型输入）"""
    feat_dict = extract_all_features(df, **kwargs)
    return pd.Series(feat_dict)


def get_feature_names() -> list:
    """获取所有特征名称列表"""
    import inspect
    # 创建一个最小的DataFrame用于提取特征列名
    n = 100
    x = np.linspace(0, 2 * np.pi, n)
    fake = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=n, freq="B"),
        "open": np.sin(x) + 10,
        "high": np.sin(x) + 10.05,
        "low": np.sin(x) + 9.95,
        "close": np.sin(x) + 10,
        "volume": np.abs(np.cos(x) * 1e7 + 1e7),
    })
    feat = extract_all_features(fake)
    return sorted(feat.keys())

