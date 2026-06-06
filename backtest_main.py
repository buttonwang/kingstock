#!/usr/bin/env python
"""A股智能选股系统 - 回测入口

运行回测程序，验证选股逻辑的有效性。

用法:
    python backtest_main.py                    # 默认近1年，首次运行（含预加载）
    python backtest_main.py --skip-preload     # 已有缓存时跳过预加载
    python backtest_main.py --start 20250101 --end 20251231  # 自定义范围

输出:
    data/output/backtest_{start}_{end}.csv          # 详细信号记录
    data/output/backtest_summary_{start}_{end}.csv  # 统计摘要
"""

import sys
import os

# 确保项目根目录在 sys.path 中
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def main():
    """回测入口函数"""
    # 延迟导入，避免影响 CLI --help 速度
    from src.backtest.backtest_engine import BacktestEngine
    import argparse

    parser = argparse.ArgumentParser(
        description="选股逻辑回测工具 - 验证智能选股系统的历史表现",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python backtest_main.py\n"
            "  python backtest_main.py --start 20250101 --end 20251231\n"
            "  python backtest_main.py --skip-preload\n"
        ),
    )
    parser.add_argument(
        "--start", type=str, default=None,
        help="回测起始日 YYYYMMDD（默认 1 年前）",
    )
    parser.add_argument(
        "--end", type=str, default=None,
        help="回测截止日 YYYYMMDD（默认今天）",
    )
    parser.add_argument(
        "--skip-preload", action="store_true",
        help="跳过预加载阶段，使用已有缓存数据（仅首次需要预加载）",
    )

    args = parser.parse_args()

    print("=" * 60)
    print("  选股逻辑回测系统")
    print("=" * 60)

    engine = BacktestEngine(start_date=args.start, end_date=args.end)

    if not args.skip_preload:
        print("\n[阶段1/3] 预加载数据（约15-30分钟，仅首次需要）")
        print("  数据来源: 东方财富")
        engine.preload_all_data()
    else:
        print("\n[阶段1/3] 从缓存加载已有数据...")
        engine._load_from_cache()

    print("\n[阶段2/3] 执行回测模拟...")
    engine.run()

    print("\n[阶段3/3] 生成报告...")
    engine.print_summary()
    path = engine.save_results()

    print(f"\n详细结果已保存: {path}" if path else "")
    print("=" * 60)
    print("  回测完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
