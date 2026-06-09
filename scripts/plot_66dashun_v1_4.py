"""66大顺 V1.4 回测脚本

V1.0 + V1.1 融合方案（方案B）:
- 轨道A: MACD(放宽) ∩ (ZJTJ ∪ KDJ) → Pure Hold（V1.0执行风格）
- 轨道B: ZJTJ ∩ KDJ（绕过MACD）→ ATR止损 + 移动止盈（扩容信号源）
- MACD放宽：去掉成交量确认、零轴限制、DIF环比上升
- 目标: 接近V1.1年化(27.26%)，回撤控制在10%左右

Version: 1.4
Date: 2026-06-06
对比基准: V1.0 (16.28%年化, 回撤-5.98%)
         V1.1 (27.26%年化, 回撤-28.68%)
         V1.2 (23.47%年化, 回撤-34.72%)
         V1.3 (10.08%年化, 回撤-6.32%)
"""

import os, sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

from src.data_fetcher import DataFetcher
from src.market_state import get_market_state, is_tradeable, get_ml_min_threshold
from config.settings import RPS_PERIOD, RPS_TOP_N, RPS_TOP_N_STRICT
from src.indicators.rps import calculate_sector_rps, get_top_sectors
from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj
from src.indicators.macd import calculate_macd
from src.portfolio_manager import _get_price_at_date, _calc_atr

start_date, end_date = "20230601", "20260603"

# ── V1.4 配置（双轨信号融合方案） ──
V1_4_CONFIG = {
    # 轨道A：MACD(放宽) ∩ (ZJTJ ∪ KDJ)
    'track_a': {
        'ml_min': 10,
        'enhanced_rules_min': 1,
        'hold_days': {13: 7, 11: 5, 10: 3},  # V1.0标准
        'max_position_pct': 0.05,
    },
    # 轨道B：ZJTJ ∩ KDJ（绕过MACD扩容）
    'track_b': {
        'ml_min': 12,            # 比V1.3银信号松弛（原13）
        'enhanced_rules_min': 1,
        'hold_days': {13: 7, 11: 5},
        'max_position_pct': 0.03,
        'atr_stop_mult': 2.0,
        'atr_trail_mult': 1.5,
        'price_stop_pct': -10.0,
        'max_hold_days': 7,
    },
    # 市场状态仓位乘数
    'market_mult': {
        0: {'strong': 1.0, 'choppy': 0.5, 'weak_reduced': 0.0, 'weak': 0.0},
        1: {'strong': 1.0, 'choppy': 0.0, 'weak_reduced': 0.0, 'weak': 0.0},
    },
    'weak_market_ml_threshold': 14,
}

print("=" * 80)
print("66大顺 V1.4 回测开始 — V1.0+V1.1融合方案B")
print("=" * 80)
print(f"\n优化配置:")
print(f"  【轨道A】MACD(放宽)∩(ZJTJ∪KDJ) → Pure Hold（ML≥10, 增强≥1, 仓位5%）")
print(f"  【轨道B】ZJTJ∩KDJ（绕过MACD）→ ATR止损（ML≥12, 增强≥1, 仓位3%）")
print(f"  MACD放宽: 去掉成交量确认、零轴限制、DIF环比上升")
print(f"  市场门控: 强市→全开 | 震荡→仅A轨道50% | 弱市→不交易")
print()

# ── 加载数据 ──
fetcher = DataFetcher()
lookback = pd.Timestamp(start_date) - pd.Timedelta(days=250)
lookfwd = pd.Timestamp(end_date) + pd.Timedelta(days=120)
fmt_start = lookback.strftime("%Y-%m-%d")
fmt_end = lookfwd.strftime("%Y-%m-%d")

sec_df = fetcher._sql_to_df("SELECT DISTINCT sector_name, sector_type FROM sector_daily")
sector_daily = {}
for _, row in sec_df.iterrows():
    df = fetcher._sql_to_df(
        "SELECT date, close, change_pct FROM sector_daily WHERE sector_name=? AND sector_type=? "
        "AND date>=? AND date<=? ORDER BY date",
        params=(row["sector_name"], row["sector_type"], fmt_start, fmt_end),
    )
    if not df.empty:
        sector_daily[row["sector_name"]] = df

cons_df = fetcher._sql_to_df("SELECT sector_name, code, name FROM sector_constituents")
sector_constituents, stock_name_map = {}, {}
for _, row in cons_df.iterrows():
    n = row["sector_name"]
    sector_constituents.setdefault(n, set()).add(row["code"])
    stock_name_map[row["code"]] = row["name"]

all_codes = set()
for codes in sector_constituents.values():
    all_codes.update(codes)
stock_daily = {}
for code in all_codes:
    df = fetcher._sql_to_df(
        "SELECT date, open, high, low, close, volume, turnover_rate FROM stock_daily "
        "WHERE code=? AND date>=? AND date<=? ORDER BY date",
        params=(code, fmt_start, fmt_end),
    )
    if not df.empty and len(df) >= 60:
        stock_daily[code] = df

fetcher.close()

# 基准指数
import akshare as ak
index_map = {"sh000300": "沪深300", "sh000906": "中证800"}
benchmark_data = {}
start_ts = pd.Timestamp(start_date)
end_ts = pd.Timestamp(end_date)
for symbol, name in index_map.items():
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        df["date"] = pd.to_datetime(df["date"])
        df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)]
        if not df.empty:
            df = df.sort_values("date").reset_index(drop=True)
            benchmark_data[name] = df
            print(f"{name}: {len(df)} rows")
    except Exception as e:
        print(f"{name} error: {e}")

# 交易日列表
all_dates = set()
for df in sector_daily.values():
    all_dates.update(df["date"].tolist())
trading_dates = sorted(d for d in all_dates
                       if pd.Timestamp(start_date) <= pd.Timestamp(d) <= pd.Timestamp(end_date))

# ── V1.4 放宽版MACD过滤 ──
def filter_by_macd_relaxed(stock_dict):
    """放宽版MACD过滤：去掉成交量确认、零轴限制、DIF环比上升，
    只保留DIF上穿DEA（金叉本质）
    
    预期信号量是原版MACD的2-3倍
    """
    result = set()
    for code, df in stock_dict.items():
        try:
            if df is None or len(df) < 35:
                continue
            df_macd = calculate_macd(df)
            if len(df_macd) < 2:
                continue
            today = df_macd.iloc[-1]
            yesterday = df_macd.iloc[-2]
            if pd.isna(today['dif']) or pd.isna(today['dea']):
                continue
            if pd.isna(yesterday['dif']) or pd.isna(yesterday['dea']):
                continue
            # 核心条件：DIF上穿DEA（金叉），去掉所有额外限制
            if today['dif'] > today['dea'] and yesterday['dif'] <= yesterday['dea']:
                result.add(code)
        except Exception:
            continue
    return result

# ── V1.4 双轨信号生成 ──
from src.scoring import compute_total_score
from src.indicators.kdj import calculate_kdj
from src.indicators.zjtj import calculate_zjtj
from src.indicators.enhanced_rules import check_all_enhanced_rules
from src.ml import ml_scorer
from config.settings import RPS_TOP_N_STRICT, SCORE_THRESHOLD_STRONG, SCORE_THRESHOLD_CHOPPY

def check_weekly_trend(df):
    """周线MACD多头确认"""
    if df is None or len(df) < 60:
        return True
    df_copy = df.copy()
    df_copy["date"] = pd.to_datetime(df_copy["date"])
    weekly = df_copy.resample("W", on="date").agg({
        "close": "last", "high": "max", "low": "min",
        "open": "first", "volume": "sum",
    }).dropna()
    if len(weekly) < 12:
        return True
    try:
        macd_w = calculate_macd(weekly)
        if macd_w is not None and len(macd_w) > 0:
            last = macd_w.iloc[-1]
            dif = last.get("dif", 0)
            dea = last.get("dea", 0)
            if pd.notna(dif) and pd.notna(dea):
                return dif > dea
    except Exception:
        pass
    return True

ml_avail = ml_scorer.is_available()
total_dates = len(trading_dates)

print(f"\n开始生成信号（V1.4双轨融合模式）...")
print(f"总交易日: {total_dates}")

track_a_signals, track_b_signals = [], []

for di, date_str in enumerate(trading_dates):
    if di % max(1, total_dates // 20) == 0:
        print(f"  进度 {di}/{total_dates} ({100*di//total_dates}%)")

    fmt_date = pd.Timestamp(date_str)
    sdata = {}
    for name, df in sector_daily.items():
        sub = df[pd.to_datetime(df["date"]) <= fmt_date]
        if len(sub) >= RPS_PERIOD:
            sdata[name] = sub
    if not sdata:
        continue
    try:
        rps_df = calculate_sector_rps(sdata, period=RPS_PERIOD)
        top_sectors = get_top_sectors(rps_df, top_n=RPS_TOP_N_STRICT)
    except Exception:
        continue
    if rps_df.empty:
        continue
    rps_rank_map = dict(zip(rps_df["sector_name"], rps_df["rps_rank"]))
    code_to_sector = {}
    for name in top_sectors:
        for c in sector_constituents.get(name, set()):
            code_to_sector.setdefault(c, name)
    stock_dict = {}
    for code in code_to_sector:
        df = stock_daily.get(code)
        if df is None:
            continue
        sub = df[pd.to_datetime(df["date"]) <= fmt_date].copy()
        if len(sub) >= 60:
            stock_dict[code] = sub
    if not stock_dict:
        continue

    # ── 双轨信号源获取 ──
    macd_relaxed_codes = filter_by_macd_relaxed(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)
    kdj_codes = filter_by_kdj(stock_dict)

    # 轨道A: MACD放宽 ∩ (ZJTJ ∪ KDJ)
    track_a_codes = macd_relaxed_codes & (zjtj_codes | kdj_codes)
    # 轨道B: ZJTJ ∩ KDJ（绕过MACD，扣除已在A中的避免重复）
    track_b_codes = (zjtj_codes & kdj_codes) - track_a_codes

    # 市场状态判断
    past_returns = []
    for code, df in stock_dict.items():
        closes = df["close"].values
        if len(closes) >= 11:
            past_returns.append((closes[-1] / closes[-11] - 1) * 100)
    market_10d_past = np.mean(past_returns) if past_returns else 0
    market_state_raw = get_market_state(market_10d_past)
    if not is_tradeable(market_state_raw):
        market_state = "weak_reduced"
    else:
        market_state = market_state_raw

    # ── 轨道A：MACD(放宽)∩(ZJTJ∪KDJ) ──
    cfg_a = V1_4_CONFIG['track_a']
    for code in track_a_codes:
        df = stock_dict[code]
        sector = code_to_sector[code]
        rps_rank = rps_rank_map.get(sector, RPS_TOP_N)
        ml_val = None
        if ml_avail:
            try:
                ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
            except Exception:
                pass

        # ML门槛（V1.0标准）
        if V1_4_CONFIG.get('weak_market_relax', False) and not is_tradeable(market_state_raw):
            ml_threshold = V1_4_CONFIG.get('weak_market_ml_threshold', 14)
        else:
            ml_threshold = get_ml_min_threshold(market_state_raw)
        ml_threshold = max(ml_threshold, cfg_a['ml_min'])
        if ml_val is not None and ml_val < ml_threshold:
            continue

        try:
            dm = calculate_macd(df)
            dk = calculate_kdj(df)
            dz = calculate_zjtj(df)
            scores = compute_total_score(dm, dk, dz, rps_rank, ml_score=ml_val)

            enh_info = check_all_enhanced_rules(df)
            if enh_info["rules_passed"] < cfg_a['enhanced_rules_min']:
                continue
        except Exception:
            continue

        # 弱势市场ML门槛收紧
        if market_state == "weak_reduced" and (ml_val is None or ml_val < V1_4_CONFIG['weak_market_ml_threshold']):
            continue

        track_a_signals.append({
            "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
            "score_ml": scores.get("score_ml", 0),
            "total_score": scores.get("total_score", 0),
            "max_score": scores.get("max_score", 100),
            "market_state": market_state,
            "track": 0,
        })

    # ── 轨道B：ZJTJ∩KDJ（绕过MACD，仅强势市场） ──
    cfg_b = V1_4_CONFIG['track_b']
    for code in track_b_codes:
        # 仅强势市场开启轨道B
        if market_state not in ("strong",):
            continue

        df = stock_dict[code]
        sector = code_to_sector[code]
        rps_rank = rps_rank_map.get(sector, RPS_TOP_N)
        ml_val = None
        if ml_avail:
            try:
                ml_val = ml_scorer.predict_score(df, rps_rank=rps_rank, rps_top_n=RPS_TOP_N)
            except Exception:
                pass

        # 轨道B ML≥12（比V1.3的13放宽）
        if ml_val is None or ml_val < cfg_b['ml_min']:
            continue

        try:
            dm = calculate_macd(df)
            dk = calculate_kdj(df)
            dz = calculate_zjtj(df)
            scores = compute_total_score(dm, dk, dz, rps_rank, ml_score=ml_val)

            enh_info = check_all_enhanced_rules(df)
            if enh_info["rules_passed"] < cfg_b['enhanced_rules_min']:
                continue
        except Exception:
            continue

        track_b_signals.append({
            "date": date_str, "code": code, "name": stock_name_map.get(code, ""),
            "score_ml": scores.get("score_ml", 0),
            "total_score": scores.get("total_score", 0),
            "max_score": scores.get("max_score", 100),
            "market_state": market_state,
            "track": 1,
        })

df_a = pd.DataFrame(track_a_signals)
df_b = pd.DataFrame(track_b_signals)
df_all = pd.concat([df_a, df_b], ignore_index=True) if track_a_signals or track_b_signals else pd.DataFrame()
print(f"\n轨道A: {len(df_a)}, 轨道B: {len(df_b)}, 合计: {len(df_all)}")
if not df_all.empty:
    if not df_a.empty:
        print(f"  轨道A track: {df_a['track'].value_counts().to_dict()}")
        print(f"  轨道A score_ml: mean={df_a['score_ml'].mean():.1f}, min={df_a['score_ml'].min():.1f}")
    if not df_b.empty:
        print(f"  轨道B track: {df_b['track'].value_counts().to_dict()}")
        print(f"  轨道B score_ml: mean={df_b['score_ml'].mean():.1f}, min={df_b['score_ml'].min():.1f}")
    df_all.to_csv("data/output/v1_4_signals_debug.csv", index=False, encoding="utf-8-sig")

# ────────────────────────────────────────────────────────────
# V1.4 组合模拟（双轨信号融合）
# ────────────────────────────────────────────────────────────
print(f"\n开始组合模拟（V1.4双轨融合模式）...")

def simulate_v14_portfolio(signals_df, stock_daily, trading_dates, initial_capital=1_000_000):
    """V1.4 双轨信号组合模拟
    
    轨道A(0): Pure Hold 到期卖出（V1.0风格）
    轨道B(1): ATR止损 + 移动止盈 + 时间止损（V1.3银风格）
    统一资金池，按轨道区分退出规则
    """
    if signals_df.empty:
        return {"total_trades": 0, "final_value": initial_capital, "total_return": 0}

    signals_df = signals_df.copy()
    signals_df["date"] = signals_df["date"].astype(str)
    signals = signals_df.to_dict("records")
    signal_lookup = defaultdict(list)
    for sig in signals:
        signal_lookup[sig["date"]].append(sig)

    all_dates = sorted(trading_dates) if trading_dates else sorted(signal_lookup.keys())
    if not all_dates:
        return {"total_trades": 0}

    date_to_idx = {d: i for i, d in enumerate(all_dates)}
    active = {}
    closed_trades = []
    daily_nav = []
    capital = initial_capital

    for date_str in all_dates:
        today_signals = signal_lookup.get(date_str, [])
        today_idx = date_to_idx.get(date_str)

        # ── 检查持仓退出 ──
        to_close = []
        for code, pos in list(active.items()):
            stock_df = stock_daily.get(code)
            if stock_df is None or stock_df.empty:
                exit_price = pos["entry_price"]
                to_close.append((code, pos, exit_price, date_str, "NO_DATA"))
                continue
            current_price = _get_price_at_date(stock_df, date_str)
            if current_price <= 0:
                current_price = pos["entry_price"]

            # 更新最高价
            if current_price > pos.get("peak_price", pos["entry_price"]):
                pos["peak_price"] = current_price

            ret = (current_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0
            track = pos.get("track", 0)

            if track == 0:
                # ── 轨道A：Pure Hold 到期卖出 ──
                exit_idx = pos.get("exit_idx")
                if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                    to_close.append((code, pos, current_price, date_str, "TRACK_A_HOLD"))
            else:
                # ── 轨道B：ATR止损 + 移动止盈 + 时间止损 ──
                atr = pos.get("atr_value", 0)
                exit_reason = None

                # 1) ATR硬止损
                if atr > 0:
                    stop_price = pos["entry_price"] - V1_4_CONFIG['track_b']['atr_stop_mult'] * atr
                    if current_price <= stop_price:
                        exit_reason = "TRACK_B_ATR_STOP"

                # 2) 纯价格止损
                if exit_reason is None:
                    if ret <= V1_4_CONFIG['track_b']['price_stop_pct']:
                        exit_reason = "TRACK_B_PRICE_STOP"

                # 3) ATR跟踪止损
                if exit_reason is None and atr > 0:
                    trail_pct = V1_4_CONFIG['track_b']['atr_trail_mult'] * atr / pos["entry_price"]
                    if pos.get("peak_price", pos["entry_price"]) > pos["entry_price"] * 1.02:
                        dd = (pos["peak_price"] - current_price) / pos["peak_price"]
                        if dd >= trail_pct:
                            exit_reason = "TRACK_B_TRAIL_STOP"

                if exit_reason:
                    to_close.append((code, pos, current_price, date_str, exit_reason))
                    continue

                # 4) 时间止损
                exit_idx = pos.get("exit_idx")
                if today_idx is not None and exit_idx is not None and today_idx >= exit_idx:
                    to_close.append((code, pos, current_price, date_str, "TRACK_B_TIME_STOP"))

        # 执行卖出
        for code, pos, exit_price, exit_date, reason in to_close:
            ret = (exit_price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0
            trade_pnl = pos["position_size"] * ret / 100
            capital += pos["position_size"] + trade_pnl
            closed_trades.append({
                "code": code, "entry_date": pos["entry_date"],
                "exit_date": exit_date, "entry_price": pos["entry_price"],
                "exit_price": exit_price, "position_size": pos["position_size"],
                "return_pct": round(ret, 2), "pnl": round(trade_pnl, 2),
                "exit_reason": reason,
                "entry_score_ml": pos.get("entry_score_ml", 0),
                "track": pos.get("track", 0),
                "held_days": pos.get("held_days", 0),
            })
            del active[code]

        # ── 当日新信号买入 ──
        if today_signals:
            # 按track分组，每个track内按score_ml降序
            track_0_today = sorted([s for s in today_signals if s["track"] == 0],
                                    key=lambda x: x.get("score_ml", 0), reverse=True)
            track_1_today = sorted([s for s in today_signals if s["track"] == 1],
                                    key=lambda x: x.get("score_ml", 0), reverse=True)

            ms = today_signals[0].get("market_state", "strong") if today_signals else "strong"

            for sig in track_0_today:
                code = sig["code"]
                if code in active:
                    continue
                ms_sig = sig.get("market_state", "strong")

                mult = V1_4_CONFIG['market_mult'][0].get(ms_sig, 0)
                if mult <= 0:
                    continue

                max_pos_pct = V1_4_CONFIG['track_a']['max_position_pct']
                pos_size = capital * max_pos_pct * mult
                pos_size = min(pos_size, capital * 0.95)
                if pos_size <= 0:
                    continue

                stock_df = stock_daily.get(code)
                if stock_df is None:
                    continue
                entry_price = _get_price_at_date(stock_df, date_str)
                if entry_price <= 0:
                    continue

                capital -= pos_size

                ml = sig.get("score_ml", 10)
                hd_map = V1_4_CONFIG['track_a']['hold_days']
                hold_days = 3
                for threshold, days in sorted(hd_map.items(), reverse=True):
                    if ml >= threshold:
                        hold_days = days
                        break

                exit_idx = today_idx + hold_days if today_idx is not None else None

                active[code] = {
                    "entry_date": date_str,
                    "entry_price": entry_price,
                    "position_size": pos_size,
                    "entry_score_ml": ml,
                    "entry_total_score": sig.get("total_score", 0),
                    "exit_idx": exit_idx,
                    "held_days": hold_days,
                    "peak_price": entry_price,
                    "track": 0,
                }

            # 轨道B：仅强势市场，每日最多3只
            if ms == "strong" and track_1_today:
                max_b = min(len(track_1_today), 3)
                for sig in track_1_today[:max_b]:
                    code = sig["code"]
                    if code in active:
                        continue

                    mult = V1_4_CONFIG['market_mult'][1].get(ms, 0)
                    if mult <= 0:
                        continue

                    max_pos_pct = V1_4_CONFIG['track_b']['max_position_pct']
                    pos_size = capital * max_pos_pct * mult
                    pos_size = min(pos_size, capital * 0.95)
                    if pos_size <= 0:
                        continue

                    stock_df = stock_daily.get(code)
                    if stock_df is None:
                        continue
                    entry_price = _get_price_at_date(stock_df, date_str)
                    if entry_price <= 0:
                        continue

                    capital -= pos_size

                    ml = sig.get("score_ml", 12)
                    hd_map = V1_4_CONFIG['track_b']['hold_days']
                    hold_days = 5
                    for threshold, days in sorted(hd_map.items(), reverse=True):
                        if ml >= threshold:
                            hold_days = days
                            break

                    exit_idx = today_idx + hold_days if today_idx is not None else None
                    atr_val = _calc_atr(stock_df)

                    active[code] = {
                        "entry_date": date_str,
                        "entry_price": entry_price,
                        "position_size": pos_size,
                        "entry_score_ml": ml,
                        "entry_total_score": sig.get("total_score", 0),
                        "exit_idx": exit_idx,
                        "held_days": hold_days,
                        "peak_price": entry_price,
                        "track": 1,
                        "atr_value": atr_val,
                    }

        # 每日净值
        pos_values = sum(p["position_size"] for p in active.values())
        daily_nav.append({"date": date_str, "nav": round(capital + pos_values, 2)})

    # 强制平仓
    for code, pos in list(active.items()):
        stock_df = stock_daily.get(code)
        if stock_df is not None and not stock_df.empty:
            exit_price = _get_price_at_date(stock_df, daily_nav[-1]["date"]) if daily_nav else pos["entry_price"]
        else:
            exit_price = pos["entry_price"]
        if exit_price <= 0:
            exit_price = pos["entry_price"]
        ret = (exit_price / pos["entry_price"] - 1) * 100
        trade_pnl = pos["position_size"] * ret / 100
        capital += pos["position_size"] + trade_pnl
        closed_trades.append({
            "code": code, "entry_date": pos["entry_date"],
            "exit_date": daily_nav[-1]["date"] if daily_nav else "UNKNOWN",
            "entry_price": pos["entry_price"], "exit_price": exit_price,
            "position_size": pos["position_size"], "return_pct": round(ret, 2),
            "pnl": round(trade_pnl, 2), "exit_reason": "FORCED_CLOSE",
            "entry_score_ml": pos.get("entry_score_ml", 0),
            "track": pos.get("track", 0),
            "held_days": pos.get("held_days", 0),
        })

    final_value = capital
    total_return = (final_value / initial_capital - 1) * 100

    nav_series = pd.Series([d["nav"] for d in daily_nav])
    n_days = len(daily_nav)
    ann_return = (final_value / initial_capital) ** (250 / max(n_days, 1)) - 1 if n_days > 0 else 0

    peak = nav_series.cummax()
    drawdowns = (nav_series - peak) / peak * 100
    max_dd = drawdowns.min() if len(drawdowns) > 0 else 0

    daily_returns = nav_series.pct_change().dropna()
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe = (daily_returns.mean() - 0.02 / 250) / daily_returns.std() * np.sqrt(250)
    else:
        sharpe = 0

    closed_df = pd.DataFrame(closed_trades)
    if not closed_df.empty:
        wins = closed_df[closed_df["return_pct"] > 0]
        losses = closed_df[closed_df["return_pct"] <= 0]
        win_rate = len(wins) / len(closed_df) * 100 if len(closed_df) > 0 else 0
        avg_win = wins["return_pct"].mean() if len(wins) > 0 else 0
        avg_loss = abs(losses["return_pct"].mean()) if len(losses) > 0 else 0
        profit_ratio = avg_win / avg_loss if avg_loss > 0 else 0
        profit_factor = (wins["pnl"].sum() / abs(losses["pnl"].sum())) if losses["pnl"].sum() != 0 else float("inf")
    else:
        win_rate = profit_ratio = profit_factor = 0

    exit_reasons = {}
    for trade in closed_trades:
        r = trade["exit_reason"]
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    return {
        "final_value": round(final_value, 2),
        "total_return": round(total_return, 2),
        "ann_return": round(ann_return * 100, 2),
        "max_drawdown": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "win_rate": round(win_rate, 1),
        "profit_ratio": round(profit_ratio, 2),
        "profit_factor": round(profit_factor, 2),
        "total_trades": len(closed_trades),
        "exit_reasons": exit_reasons,
        "daily_nav": daily_nav,
        "closed_trades": closed_trades,
        "tier_breakdown": None,
    }

result = simulate_v14_portfolio(df_all, stock_daily, trading_dates)

# ── 按轨道统计 ──
closed_df_full = pd.DataFrame(result.get("closed_trades", []))
track_stats = {}
for track, label in [(0, "轨道A"), (1, "轨道B")]:
    sub = closed_df_full[closed_df_full["track"] == track] if not closed_df_full.empty else pd.DataFrame()
    if not sub.empty:
        wins = sub[sub["return_pct"] > 0]
        losses = sub[sub["return_pct"] <= 0]
        avg_w = wins["return_pct"].mean() if len(wins) > 0 else 0
        avg_l = abs(losses["return_pct"].mean()) if len(losses) > 0 else 0
        track_stats[label] = {
            "trades": len(sub),
            "win_rate": len(wins) / len(sub) * 100,
            "avg_win": round(avg_w, 2),
            "avg_loss": round(avg_l, 2),
            "profit_ratio": round(avg_w / avg_l, 2) if avg_l > 0 else 0,
            "total_pnl": round(sub["pnl"].sum(), 2),
        }
    else:
        track_stats[label] = {"trades": 0, "win_rate": 0, "avg_win": 0, "avg_loss": 0, "profit_ratio": 0, "total_pnl": 0}

# ── 净值曲线 ──
daily_nav_list = result["daily_nav"]
initial_capital = 1_000_000.0
raw_nav = [d["nav"] for d in daily_nav_list]
nav_series = pd.Series(
    [v / initial_capital for v in raw_nav],
    index=pd.to_datetime(trading_dates),
)
print(f"\n净值曲线长度: {len(nav_series)}")
print(f"最终净值: {nav_series.iloc[-1]:.4f}, 收益率: {result['total_return']:.2f}%")
print(f"夏普比率: {result['sharpe']:.2f}, 最大回撤: {result['max_drawdown']:.2f}%")
print(f"胜率: {result['win_rate']:.1f}%, 交易次数: {result['total_trades']}")

# ── 基准指数归一化 ──
bench_norm = {}
for name, df in benchmark_data.items():
    dates = pd.to_datetime(df["date"]).dt.tz_localize(None)
    prices = df["close"].values.astype(float)
    if len(prices) == 0:
        continue
    base_price = prices[0]
    norm_prices = prices / base_price
    ts = pd.Series(norm_prices, index=dates)
    bench_norm[name] = ts

# ── 绘图 ──
from matplotlib.dates import DateFormatter, MonthLocator

plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
plt.rcParams['axes.unicode_minus'] = False

fig, axes = plt.subplots(3, 1, figsize=(16, 14), sharex=True)
fig.suptitle("66大顺 V1.4 V1.0+V1.1融合回测曲线", fontsize=18, fontweight="bold")

# 1) 累计收益率
ax1 = axes[0]
ax1.plot(nav_series.index, (nav_series.values - 1) * 100, label="66大顺 V1.4", color="#0066CC", linewidth=2.5)
for name, ts in bench_norm.items():
    nav_idx = nav_series.index.tz_localize(None) if nav_series.index.tz else nav_series.index
    common = nav_idx.intersection(ts.index)
    if len(common) > 0:
        base_val = ts.loc[common[0]]
        ret_vals = (ts.loc[common] / base_val - 1) * 100
        ax1.plot(common, ret_vals.values, label=name, linewidth=1.5, alpha=0.8)
ax1.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
ax1.set_ylabel("累计收益率 (%)")
ax1.legend(loc="upper left", fontsize=11)
ax1.grid(True, alpha=0.3)
ax1.set_title("累计收益率曲线 (V1.4 V1.0+V1.1融合)", fontsize=14, fontweight="bold")

# 2) 回撤曲线
ax2 = axes[1]
peak = nav_series.cummax()
drawdown = (nav_series - peak) / peak * 100
ax2.fill_between(nav_series.index, drawdown.values, 0, color="#FF4444", alpha=0.5,
                 label=f"最大回撤 {result['max_drawdown']:.2f}%")
ax2.set_ylabel("回撤 (%)")
ax2.legend(loc="lower left", fontsize=11)
ax2.grid(True, alpha=0.3)
ax2.set_title("回撤曲线", fontsize=14, fontweight="bold")

# 3) 滚动夏普
ax3 = axes[2]
daily_ret = nav_series.pct_change().dropna()
window = 63
rolling_sharpe = daily_ret.rolling(window).apply(
    lambda x: (x.mean() - 0.02/250) / x.std() * np.sqrt(250) if x.std() > 0 else 0
)
ax3.plot(rolling_sharpe.index, rolling_sharpe.values, color="#4A90D9", linewidth=1.5, label=f"63日滚动夏普")
ax3.axhline(y=result["sharpe"], color="#4A90D9", linewidth=1, linestyle="--", alpha=0.7,
            label=f"全周期夏普 {result['sharpe']:.2f}")
ax3.fill_between(rolling_sharpe.index, 0, rolling_sharpe.values, where=(rolling_sharpe.values >= 0),
                 color="#4A90D9", alpha=0.3)
ax3.fill_between(rolling_sharpe.index, rolling_sharpe.values, 0, where=(rolling_sharpe.values < 0),
                 color="#FF4444", alpha=0.3)
ax3.axhline(y=0, color="gray", linewidth=0.5, linestyle="--")
ax3.set_ylabel("夏普比率")
ax3.legend(loc="upper left", fontsize=11)
ax3.grid(True, alpha=0.3)
ax3.set_title("滚动夏普比率 (63个交易日窗口)", fontsize=14, fontweight="bold")

ax3.xaxis.set_major_locator(MonthLocator(interval=2))
ax3.xaxis.set_minor_locator(MonthLocator())
ax3.xaxis.set_major_formatter(DateFormatter('%Y-%m'))
plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45, ha='right', fontsize=9)
plt.tight_layout()
fig.subplots_adjust(bottom=0.08)
output_path = os.path.join("data", "output", "66dashun_v1_4_curve.png")
plt.savefig(output_path, dpi=150, bbox_inches="tight")
print(f"\n图表已保存: {output_path}")
plt.close()

# ── 五版本对比总结 ──
print("\n" + "=" * 80)
print("66大顺 V1.4 回测结果总结 — V1.0+V1.1融合方案B")
print("=" * 80)
print(f"{'指标':<20} {'V1.0':<12} {'V1.1':<12} {'V1.2':<12} {'V1.3':<12} {'V1.4(本版)':<12}")
print("-" * 80)
tr = f"{result.get('total_return', 0):.2f}%"
ar = f"{result.get('ann_return', 0):.2f}%"
sr = f"{result.get('sharpe', 0):.2f}"
dd = f"{result.get('max_drawdown', 0):.2f}%"
wr = f"{result.get('win_rate', 0):.1f}%"
pro = f"{result.get('profit_ratio', 0):.2f}"
pf = f"{result.get('profit_factor', 0):.2f}"
tt = f"{result.get('total_trades', 0)}"
sign = f"{len(df_all)}"
print(f"{'总收益率':<20} {'46.90%':<12} {'101.40%':<12} {'84.46%':<12} {'32.17%':<12} {tr:<12}")
print(f"{'年化收益':<20} {'16.28%':<12} {'27.26%':<12} {'23.47%':<12} {'10.08%':<12} {ar:<12}")
print(f"{'夏普比率':<20} {'3.82':<12} {'1.58':<12} {'1.45':<12} {'1.21':<12} {sr:<12}")
print(f"{'最大回撤':<20} {'-5.98%':<12} {'-28.68%':<12} {'-34.72%':<12} {'-6.32%':<12} {dd:<12}")
print(f"{'胜率':<20} {'~60%':<12} {'44.8%':<12} {'41.8%':<12} {'49.4%':<12} {wr:<12}")
print(f"{'盈亏比':<20} {'2.1+':<12} {'1.59':<12} {'1.84':<12} {'1.78':<12} {pro:<12}")
print(f"{'利润因子':<20} {'N/A':<12} {'1.42':<12} {'1.47':<12} {'1.72':<12} {pf:<12}")
print(f"{'交易次数':<20} {'541':<12} {'4963':<12} {'3512':<12} {'429':<12} {tt:<12}")
print(f"{'信号总数':<20} {'551':<12} {'14223':<12} {'11570':<12} {'586':<12} {sign:<12}")

print(f"\n双轨统计:")
for label, st in track_stats.items():
    print(f"  {label}: {st['trades']}笔, 胜率{st['win_rate']:.1f}%, "
          f"均赢{st['avg_win']:.1f}%, 均亏{st['avg_loss']:.1f}%, "
          f"盈亏比{st['profit_ratio']:.2f}, 总盈亏{st['total_pnl']:.0f}元")

print(f"\n退出原因: {result.get('exit_reasons', {})}")
print("\n优化要点:")
print(f"  ✅ 轨道A MACD(放宽)∩(ZJTJ∪KDJ) — 保留V1.0执行风格，仅放宽MACD增加信号量")
print(f"  ✅ 轨道B ZJTJ∩KDJ — 绕过MACD扩容，ML≥12控制质量")
print(f"  ✅ MACD放宽: 去掉成交量确认、零轴限制、DIF环比上升")
print(f"  ✅ 强势市场: 双轨全开 | 震荡: 仅轨道A半仓 | 弱势: 不交易")
print("=" * 80)
