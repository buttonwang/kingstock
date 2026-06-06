"""参数寻优脚本 - 扫描MACD/RPS/KDJ参数组合，找出最优回测配置

用法：
    python optimize_params.py                     # 扫描全部参数
    python optimize_params.py --quick              # 快速扫描（只扫关键参数）
    python optimize_params.py --output results.csv # 输出到CSV
"""
import os
import sys
import copy
import argparse
from datetime import datetime
from itertools import product
from concurrent.futures import ProcessPoolExecutor, as_completed

import pandas as pd
import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _run_single(params: dict) -> dict:
    """在单独的进程中运行一次回测（避免参数污染）"""
    import config.settings as settings
    import importlib

    # 临时修改参数
    for k, v in params.items():
        setattr(settings, k, v)

    # 重新导入可能缓存了旧参数值的模块
    for mod_name in list(sys.modules.keys()):
        if 'src.filters' in mod_name or 'src.indicators' in mod_name:
            importlib.reload(sys.modules[mod_name])

    from src.backtest.backtest_engine import BacktestEngine, FORWARD_WINDOWS

    # 使用较短的回测区间（含1年数据即可，注意已有缓存）
    end = datetime.now().strftime("%Y%m%d")
    start_dt = datetime.now().replace(year=datetime.now().year - 1)
    start = start_dt.strftime("%Y%m%d")

    engine = BacktestEngine(start_date=start, end_date=end)
    # 从缓存加载（假设已经预加载过）
    try:
        engine._load_from_cache()
    except Exception:
        return {"status": "error", "error": "load_cache_failed", **params}

    if not engine.trading_dates:
        return {"status": "error", "error": "no_trading_dates", **params}

    engine.run()
    summary = engine.summarize()
    main_stats = summary.get("主选股", {})

    result = {"status": "ok", **params}
    for n in FORWARD_WINDOWS:
        result[f"avg_return_{n}d"] = main_stats.get(f"avg_return_{n}d")
        result[f"win_rate_{n}d"] = main_stats.get(f"win_rate_{n}d")
        result[f"signal_count"] = main_stats.get("signal_count", 0)

    return result


def scan_macd_params(quick=False):
    """扫描MACD参数组合"""
    params_list = []
    if quick:
        short_range = [9, 12, 15]
        long_range = [22, 26, 30]
        mid_range = [7, 9, 11]
    else:
        short_range = list(range(8, 21, 2))    # 8,10,12,14,16,18,20
        long_range = list(range(20, 41, 2))    # 20,22,...,40
        mid_range = list(range(5, 16, 2))      # 5,7,9,11,13,15

    for short, long_, mid in product(short_range, long_range, mid_range):
        if long_ <= short:
            continue  # LONG必须大于SHORT
        params_list.append({
            "MACD_SHORT": short,
            "MACD_LONG": long_,
            "MACD_MID": mid,
        })
    return params_list


def scan_rps_params(quick=False):
    """扫描RPS参数组合"""
    if quick:
        periods = [15, 20, 25]
        top_n = [10, 15, 20]
    else:
        periods = [10, 15, 20, 25, 30]
        top_n = [5, 10, 15, 20, 25]

    return [
        {"RPS_PERIOD": p, "RPS_TOP_N": t}
        for p, t in product(periods, top_n)
    ]


def scan_kdj_params(quick=False):
    """扫描KDJ参数组合"""
    if quick:
        n_range = [7, 9, 11]
        m_range = [2, 3, 4]
    else:
        n_range = list(range(5, 15, 2))   # 5,7,9,11,13
        m_range = list(range(2, 6))       # 2,3,4,5

    return [
        {"KDJ_N": n, "KDJ_M1": m, "KDJ_M2": m}
        for n, m in product(n_range, m_range)
    ]


def main():
    parser = argparse.ArgumentParser(description="参数寻优")
    parser.add_argument("--quick", action="store_true", help="快速扫描（只扫关键参数）")
    parser.add_argument("--output", type=str, default=None, help="输出CSV路径")
    parser.add_argument("--type", type=str, default="all",
                        choices=["all", "macd", "rps", "kdj"],
                        help="扫描类型")
    args = parser.parse_args()

    quick = args.quick
    scan_type = args.type

    all_params = []
    if scan_type in ("all", "macd"):
        all_params.extend(scan_macd_params(quick))
    if scan_type in ("all", "rps"):
        all_params.extend(scan_rps_params(quick))
    if scan_type in ("all", "kdj"):
        all_params.extend(scan_kdj_params(quick))

    print(f"参数组合数: {len(all_params)}")
    if not all_params:
        print("无参数组合需要扫描")
        return

    results = []
    total = len(all_params)

    # 顺序执行（避免进程池的序列化问题）
    for i, params in enumerate(all_params, 1):
        param_desc = ", ".join(f"{k}={v}" for k, v in params.items())
        print(f"\n[{i}/{total}] 测试: {param_desc}")
        try:
            result = _run_single(params)
            results.append(result)
            if result.get("status") == "ok":
                print(f"  信号数={result.get('signal_count',0):>4}, "
                      f"2日均涨={result.get('avg_return_2d','N/A')}, "
                      f"2日胜率={result.get('win_rate_2d','N/A')}")
            else:
                print(f"  ❌ {result.get('error', 'unknown')}")
        except Exception as e:
            print(f"  ❌ Error: {e}")
            continue

    # 汇总结果
    if not results:
        print("无有效结果")
        return

    df = pd.DataFrame(results)
    ok_df = df[df["status"] == "ok"].copy()

    if ok_df.empty:
        print("所有测试均失败，请确保已运行过回测预加载(backtest_main.py)")
        return

    # 按2日胜率排序
    sort_col = "win_rate_2d"
    if sort_col in ok_df.columns:
        ok_df = ok_df.sort_values(sort_col, ascending=False).reset_index(drop=True)

    print("\n" + "=" * 80)
    print("参数寻优结果 TOP 20（按2日胜率降序）")
    print("=" * 80)

    display_cols = [c for c in ok_df.columns
                    if c not in ("status",)]
    top20 = ok_df.head(20)[display_cols]
    pd.set_option('display.max_columns', 20)
    pd.set_option('display.width', 200)
    pd.set_option('display.max_colwidth', 30)
    print(top20.to_string(index=False))

    # 按2日均涨幅排序
    if "avg_return_2d" in ok_df.columns:
        ok_df2 = ok_df.sort_values("avg_return_2d", ascending=False).reset_index(drop=True)
        print("\n" + "=" * 80)
        print("参数寻优结果 TOP 20（按2日均涨幅降序）")
        print("=" * 80)
        print(ok_df2.head(20)[display_cols].to_string(index=False))

    # 输出CSV
    output_path = args.output
    if output_path:
        ok_df.to_csv(output_path, index=False, encoding="utf-8-sig")
        print(f"\n完整结果已导出: {output_path}")

    # 推荐最优参数
    print("\n" + "=" * 80)
    print("推荐最优参数（2日胜率最高组合）:")
    print("=" * 80)
    best = ok_df.iloc[0] if not ok_df.empty else None
    if best is not None:
        param_keys = [c for c in ok_df.columns
                      if c.upper() == c and c not in ("status",)]
        for k in param_keys:
            if k in best:
                print(f"  {k} = {best[k]}")


if __name__ == "__main__":
    main()
