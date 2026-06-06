"""Walk-Forward滚动验证

将回测期分成多个滚动时间窗口，在每个窗口内：
1. 前段（训练期）：运行参数网格搜索（MACD/RPS/KDJ），找到最优参数组合
2. 后段（验证期）：用最优参数运行回测，评估样本外表现
3. 汇总各窗口结果，评估策略稳定性

用法:
    python scripts/run_walk_forward.py
    python scripts/run_walk_forward.py --start 20250501 --end 20260601 --windows 3
"""
import os
import sys
import argparse
import itertools
import json
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
from tqdm import tqdm

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from src.backtest.backtest_engine import BacktestEngine
from config.settings import OUTPUT_PATH


# ============================================================
# 参数网格（从settings导入，支持命令行覆盖）
# ============================================================

import config.settings as settings

MACD_GRID = {
    "MACD_SHORT": [5, 12, 20],
    "MACD_LONG": [20, 26, 35],
    "MACD_MID": [5, 9, 14],
}

RPS_GRID = {
    "RPS_PERIOD": [15, 20, 25],
    "RPS_TOP_N": [15, 20, 25],
}

KDJ_GRID = {
    "KDJ_N": [7, 9, 14],
    "KDJ_M1": [3, 4, 5],
    "KDJ_M2": [3, 4, 5],
}

# 使用settings中的网格（如果存在）
if hasattr(settings, 'MACD_GRID'):
    MACD_GRID = settings.MACD_GRID
if hasattr(settings, 'ZJTJ_GRID'):
    ZJTJ_GRID = settings.ZJTJ_GRID
else:
    ZJTJ_GRID = {}

# 合并参数名
ALL_PARAM_NAMES = list(MACD_GRID.keys()) + list(RPS_GRID.keys()) + list(KDJ_GRID.keys()) + list(ZJTJ_GRID.keys())


# 扩展模式：启用settings中的扩展网格
USE_SETTINGS_GRID = True


def _apply_params(params: dict):
    """临时修改 settings 模块中的参数"""
    import config.settings as settings
    for key, val in params.items():
        if hasattr(settings, key):
            setattr(settings, key, val)


def _restore_params(backup: dict):
    """恢复 settings 模块中的参数"""
    import config.settings as settings
    for key, val in backup.items():
        if hasattr(settings, key):
            setattr(settings, key, val)


def _backup_params() -> dict:
    """备份当前 settings 参数"""
    import config.settings as settings
    return {name: getattr(settings, name, None) for name in ALL_PARAM_NAMES}


def run_single_backtest(
    start_date: str,
    end_date: str,
    params: dict,
    skip_preload: bool = True,
) -> dict:
    """用指定参数运行一次回测，返回统计摘要"""
    _apply_params(params)
    try:
        engine = BacktestEngine(start_date=start_date, end_date=end_date)
        if not skip_preload:
            engine.preload_all_data()
        else:
            engine._load_from_cache()
        engine.run()
        summaries = engine.summarize()
        main_stats = summaries.get("主选股", {"signal_count": 0})

        # 提取关键指标
        result = {
            "signal_count": main_stats.get("signal_count", 0),
            "avg_return_2d": main_stats.get("avg_return_2d"),
            "win_rate_2d": main_stats.get("win_rate_2d"),
            "avg_return_10d": main_stats.get("avg_return_10d"),
            "win_rate_10d": main_stats.get("win_rate_10d"),
            "avg_return_30d": main_stats.get("avg_return_30d"),
            "win_rate_30d": main_stats.get("win_rate_30d"),
        }
        # 止损止盈
        if "sl_tp_avg_return" in main_stats:
            result["sl_tp_avg_return"] = main_stats["sl_tp_avg_return"]
            result["sl_tp_win_rate"] = main_stats["sl_tp_win_rate"]

        # 如果信号太少视为无效
        if result["signal_count"] < 3:
            result["_valid"] = False
        else:
            result["_valid"] = True

        # 综合得分：10d胜率 + 10d均涨(加权)
        score = 0.0
        if result.get("win_rate_10d") is not None:
            score += result["win_rate_10d"]
        if result.get("avg_return_10d") is not None:
            score += max(-20, min(20, result["avg_return_10d"])) * 5  # 每1%涨=5分
        result["_score"] = round(score, 1)

        return result
    finally:
        pass  # 参数在外部恢复


def grid_search(
    train_start: str,
    train_end: str,
    max_combinations: int = 30,
) -> tuple:
    """在当前训练期上做网格搜索，返回 (best_params, all_results)"""
    all_keys = ALL_PARAM_NAMES
    all_grids = list(MACD_GRID.values()) + list(RPS_GRID.values()) + list(KDJ_GRID.values()) + list(ZJTJ_GRID.values())
    all_combos = list(itertools.product(*all_grids))

    # 随机采样控制组合数
    if len(all_combos) > max_combinations:
        np.random.seed(42)
        indices = np.random.choice(len(all_combos), max_combinations, replace=False)
        all_combos = [all_combos[i] for i in indices]

    best_params = None
    best_score = -float("inf")
    all_results = []

    backup = _backup_params()
    try:
        for combo in tqdm(all_combos, desc="网格搜索"):
            params = dict(zip(all_keys, combo))
            try:
                result = run_single_backtest(train_start, train_end, params)
                all_results.append({"params": params, **result})
                if result.get("_valid") and result.get("_score", 0) > best_score:
                    best_score = result["_score"]
                    best_params = params
            except Exception as e:
                continue
    finally:
        _restore_params(backup)

    return best_params, all_results


def run_validation(
    val_start: str,
    val_end: str,
    params: dict,
) -> dict:
    """在验证期上评估指定参数的表现"""
    backup = _backup_params()
    try:
        result = run_single_backtest(val_start, val_end, params)
        return result
    finally:
        _restore_params(backup)


def build_walk_forward_windows(
    start_date: str,
    end_date: str,
    n_windows: int = 3,
) -> list:
    """构建滚动时间窗口

    返回:
        [{"train_start": ..., "train_end": ..., "val_start": ..., "val_end": ...}, ...]
    """
    import config.settings as settings

    # 先获取交易日信息
    engine = BacktestEngine(start_date=start_date, end_date=end_date)
    engine._load_from_cache()
    all_dates = sorted(engine.trading_dates)
    if len(all_dates) < 40:
        # 日期太少，缩小窗口数
        n_windows = max(1, len(all_dates) // 30)

    total_days = len(all_dates)
    window_size = total_days // n_windows

    windows = []
    for i in range(n_windows):
        train_start_idx = i * window_size
        train_end_idx = train_start_idx + int(window_size * 0.6)
        val_start_idx = train_end_idx + 1
        val_end_idx = min(val_start_idx + int(window_size * 0.4), total_days - 1)

        if val_end_idx - val_start_idx < 5:
            continue

        windows.append({
            "train_start": all_dates[train_start_idx],
            "train_end": all_dates[train_end_idx],
            "val_start": all_dates[val_start_idx],
            "val_end": all_dates[val_end_idx],
        })

    return windows


def run_walk_forward(
    start_date: str,
    end_date: str,
    n_windows: int = 3,
    max_combinations: int = 30,
) -> dict:
    """完整 Walk-Forward 验证流程

    返回:
        {
            "windows": [...],
            "summary": {"avg_val_score": ..., "stability": ...},
        }
    """
    windows = build_walk_forward_windows(start_date, end_date, n_windows)
    if not windows:
        print("错误：无法构建有效的滚动窗口")
        return {}

    print(f"Walk-Forward 滚动验证: {len(windows)} 个窗口")
    print(f"回测区间: {start_date} ~ {end_date}")
    print("=" * 60)

    window_results = []
    all_val_scores = []

    for i, w in enumerate(windows):
        print(f"\n--- 窗口 {i+1}/{len(windows)} ---")
        print(f"  训练期: {w['train_start']} ~ {w['train_end']}")
        print(f"  验证期: {w['val_start']} ~ {w['val_end']}")

        # Step 1: 训练期网格搜索
        print(f"  网格搜索中... ({max_combinations} 组合)")
        best_params, _ = grid_search(w["train_start"], w["train_end"], max_combinations)

        if best_params is None:
            print("  未找到有效参数组合，跳过")
            continue
        print(f"  最优参数: {best_params}")

        # Step 2: 验证期评估
        val_result = run_validation(w["val_start"], w["val_end"], best_params)
        val_result["window"] = i + 1
        val_result["train_start"] = w["train_start"]
        val_result["train_end"] = w["train_end"]
        val_result["val_start"] = w["val_start"]
        val_result["val_end"] = w["val_end"]
        val_result["params"] = best_params
        window_results.append(val_result)

        print(f"  验证期结果: 信号={val_result.get('signal_count',0)}, "
              f"10d均涨={val_result.get('avg_return_10d','N/A')}, "
              f"10d胜率={val_result.get('win_rate_10d','N/A')}%")
        if val_result.get("sl_tp_avg_return") is not None:
            print(f"  止损止盈: 均收益={val_result['sl_tp_avg_return']}%, "
                  f"胜率={val_result['sl_tp_win_rate']}%")

        score_10d = val_result.get("avg_return_10d") or 0
        all_val_scores.append(score_10d)

    # 汇总
    summary = {}
    if all_val_scores:
        summary["avg_val_return_10d"] = round(np.mean(all_val_scores), 2)
        summary["std_val_return_10d"] = round(np.std(all_val_scores), 2)
        summary["stability"] = round(
            np.mean(all_val_scores) / (np.std(all_val_scores) + 0.01), 2
        )
        summary["positive_windows"] = sum(1 for s in all_val_scores if s > 0)
        summary["total_windows"] = len(all_val_scores)

    # 输出汇总
    print("\n" + "=" * 60)
    print("Walk-Forward 汇总")
    print("=" * 60)
    if summary:
        print(f"  跨窗口平均10d收益: {summary['avg_val_return_10d']:+.2f}%")
        print(f"  跨窗口标准差: {summary['std_val_return_10d']:.2f}%")
        print(f"  稳定性(均值/标准差): {summary['stability']:.2f}")
        print(f"  正收益窗口: {summary['positive_windows']}/{summary['total_windows']}")
    print("=" * 60)

    # 自动更新：选择最优窗口的参数更新settings.py
    if window_results:
        # 按10d均收益排序，选择最好的窗口
        best_window = max(window_results, key=lambda w: w.get("avg_return_10d", -999) or -999)
        best_params = best_window.get("params", {})
        if best_params:
            print(f"\n最优窗口 #{best_window['window']}: 10d收益={best_window.get('avg_return_10d', 'N/A')}%")
            print(f"最优参数: {best_params}")

            # 可选：自动更新settings.py
            _update_settings_file(best_params)
            _save_param_history(best_params, window_results, summary)

    return {
        "windows": window_results,
        "summary": summary,
    }


def save_walk_forward_results(results: dict, start_date: str, end_date: str):
    """保存 Walk-Forward 结果"""
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    range_str = f"{start_date}_{end_date}"

    # JSON 完整结果
    json_path = os.path.join(OUTPUT_PATH, f"walk_forward_{range_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    # CSV 窗口明细
    if results.get("windows"):
        rows = []
        for w in results["windows"]:
            row = {
                "window": w["window"],
                "train_start": w["train_start"],
                "train_end": w["train_end"],
                "val_start": w["val_start"],
                "val_end": w["val_end"],
                "params": str(w.get("params", {})),
                "signal_count": w.get("signal_count", 0),
                "avg_return_2d": w.get("avg_return_2d"),
                "win_rate_2d": w.get("win_rate_2d"),
                "avg_return_10d": w.get("avg_return_10d"),
                "win_rate_10d": w.get("win_rate_10d"),
                "avg_return_30d": w.get("avg_return_30d"),
                "win_rate_30d": w.get("win_rate_30d"),
                "sl_tp_avg_return": w.get("sl_tp_avg_return"),
                "sl_tp_win_rate": w.get("sl_tp_win_rate"),
            }
            rows.append(row)
        df = pd.DataFrame(rows)
        csv_path = os.path.join(OUTPUT_PATH, f"walk_forward_{range_str}.csv")
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"Walk-Forward 明细已导出: {csv_path}")

    print(f"Walk-Forward 完整结果已导出: {json_path}")


def _update_settings_file(best_params: dict, source: str = "walk_forward"):
    """更新settings.py为最优参数

    将参数写入到config/settings.py文件，使后续运行使用这些参数。
    """
    settings_path = os.path.join(BASE_DIR, "config", "settings.py")
    if not os.path.exists(settings_path):
        print(f"settings.py 不存在: {settings_path}")
        return

    print(f"\n自动更新settings.py 最优参数...")

    # 读取settings.py
    with open(settings_path, "r", encoding="utf-8") as f:
        content = f.read()

    updated = 0
    for key, val in best_params.items():
        # 只更新 settings 中已存在的参数
        if hasattr(settings, key):
            old_val = getattr(settings, key)
            if old_val != val:
                # 构造正则替换
                import re
                # 匹配 key = old_val 或 key = old_val,
                pattern = re.compile(
                    rf"^(\s*{re.escape(key)}\s*=\s*){re.escape(repr(old_val))}("r"\s*$|\s*,\s*$)",
                    re.MULTILINE,
                )
                new_content, count = pattern.sub(rf"\g<1>{repr(val)}\g<2>", content)
                if count > 0:
                    content = new_content
                    print(f"  {key}: {old_val} -> {val}")
                    updated += 1

    if updated > 0:
        with open(settings_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"settings.py 已更新 {updated} 个参数")
    else:
        print("无需更新（所有参数与当前一致）")


def _save_param_history(best_params: dict, window_results: list, summary: dict):
    """保存历史参数寻优记录到CSV

    文件: data/output/param_history.csv
    """
    import csv
    from datetime import datetime

    history_path = os.path.join(OUTPUT_PATH, "param_history.csv")
    os.makedirs(OUTPUT_PATH, exist_ok=True)

    row = {
        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "windows_count": len(window_results),
        "strategy": "walk_forward",
        "avg_val_return_10d": summary.get("avg_val_return_10d", ""),
        "std_val_return_10d": summary.get("std_val_return_10d", ""),
        "stability": summary.get("stability", ""),
        "positive_windows": summary.get("positive_windows", ""),
        "total_windows": summary.get("total_windows", ""),
    }
    # 展平最优参数
    for k, v in best_params.items() if best_params else []:
        row[f"param_{k}"] = v

    # 判断文件是否存在来决定是否写表头
    file_exists = os.path.exists(history_path)
    with open(history_path, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"参数历史已记录: {history_path}")


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Walk-Forward 滚动验证")
    parser.add_argument("--start", type=str, default=None, help="起始日 YYYYMMDD")
    parser.add_argument("--end", type=str, default=None, help="截止日 YYYYMMDD")
    parser.add_argument("--windows", type=int, default=3, help="滚动窗口数 (默认3)")
    parser.add_argument("--max-combinations", type=int, default=30, help="每窗口网格组合数 (默认30)")
    parser.add_argument("--save", action="store_true", default=True, help="保存结果")
    args = parser.parse_args()

    if args.start is None:
        args.start = (datetime.now() - timedelta(days=370)).strftime("%Y%m%d")
    if args.end is None:
        args.end = datetime.now().strftime("%Y%m%d")

    results = run_walk_forward(
        start_date=args.start,
        end_date=args.end,
        n_windows=args.windows,
        max_combinations=args.max_combinations,
    )

    if args.save and results:
        save_walk_forward_results(results, args.start, args.end)
