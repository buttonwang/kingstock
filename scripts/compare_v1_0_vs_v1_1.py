"""66大顺 V1.0 vs V1.1 性能对比分析

对比两个版本的关键指标，生成详细的对比报告。

Version: 1.1
Date: 2026-06-06
"""

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)


def compare_versions(v10_result: dict, v11_result: dict, 
                     v10_signals: int, v11_signals: int) -> dict:
    """对比V1.0和V1.1的性能指标
    
    Args:
        v10_result: V1.0回测结果dict
        v11_result: V1.1回测结果dict
        v10_signals: V1.0信号数量
        v11_signals: V1.1信号数量
    
    Returns:
        对比结果dict
    """
    metrics = [
        ('total_return', '总收益率 (%)', True),
        ('ann_return', '年化收益 (%)', True),
        ('sharpe', '夏普比率', True),
        ('max_drawdown', '最大回撤 (%)', False),
        ('win_rate', '胜率 (%)', True),
        ('profit_ratio', '盈亏比', True),
        ('profit_factor', '利润因子', True),
        ('total_trades', '交易次数', True),
    ]
    
    comparison = {}
    for key, name, higher_better in metrics:
        v10_val = v10_result.get(key, 0)
        v11_val = v11_result.get(key, 0)
        
        if v10_val != 0:
            improvement = (v11_val - v10_val) / abs(v10_val) * 100
        else:
            improvement = 0
        
        comparison[key] = {
            'name': name,
            'v10': v10_val,
            'v11': v11_val,
            'improvement': improvement,
            'better': (v11_val > v10_val) if higher_better else (v11_val < v10_val),
        }
    
    # 信号数量对比
    signal_improvement = (v11_signals - v10_signals) / v10_signals * 100 if v10_signals > 0 else 0
    comparison['signals'] = {
        'name': '信号数量',
        'v10': v10_signals,
        'v11': v11_signals,
        'improvement': signal_improvement,
        'better': v11_signals > v10_signals,
    }
    
    return comparison


def print_comparison_report(comparison: dict):
    """打印对比报告
    
    Args:
        comparison: 对比结果dict
    """
    print("\n" + "=" * 80)
    print("66大顺 V1.0 vs V1.1 性能对比报告")
    print("=" * 80)
    print(f"{'指标':<15} {'V1.0':<15} {'V1.1':<15} {'改善幅度':<15} {'评价':<10}")
    print("-" * 80)
    
    for key, data in comparison.items():
        name = data['name']
        v10 = data['v10']
        v11 = data['v11']
        imp = data['improvement']
        better = "✅ 提升" if data['better'] else "❌ 下降"
        
        # 格式化数值
        if isinstance(v10, float):
            v10_str = f"{v10:.2f}"
            v11_str = f"{v11:.2f}"
            imp_str = f"{imp:+.2f}%"
        else:
            v10_str = str(v10)
            v11_str = str(v11)
            imp_str = f"{imp:+.1f}%"
        
        print(f"{name:<15} {v10_str:<15} {v11_str:<15} {imp_str:<15} {better:<10}")
    
    print("=" * 80)


def plot_comparison_chart(comparison: dict, output_path: str):
    """绘制对比图表
    
    Args:
        comparison: 对比结果dict
        output_path: 输出路径
    """
    # 选择关键指标
    metrics_to_plot = ['total_return', 'sharpe', 'max_drawdown', 'win_rate', 'signals']
    
    names = []
    v10_values = []
    v11_values = []
    
    for key in metrics_to_plot:
        if key in comparison:
            names.append(comparison[key]['name'])
            v10_values.append(comparison[key]['v10'])
            v11_values.append(comparison[key]['v11'])
    
    # 归一化处理（相对于V1.0）
    v10_normalized = [1.0] * len(names)
    v11_normalized = []
    for i in range(len(names)):
        if v10_values[i] != 0:
            v11_normalized.append(v11_values[i] / v10_values[i])
        else:
            v11_normalized.append(1.0)
    
    # 绘图
    plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    x = np.arange(len(names))
    width = 0.35
    
    bars1 = ax.bar(x - width/2, v10_normalized, width, label='V1.0', color='#FF6B35', alpha=0.8)
    bars2 = ax.bar(x + width/2, v11_normalized, width, label='V1.1', color='#00AA00', alpha=0.8)
    
    ax.set_ylabel('归一化值 (V1.0=1.0)')
    ax.set_title('66大顺 V1.0 vs V1.1 关键指标对比', fontsize=16, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(names, fontsize=11)
    ax.legend(fontsize=12)
    ax.grid(True, alpha=0.3, axis='y')
    
    # 添加数值标签
    for bar in bars1:
        height = bar.get_height()
        ax.annotate(f'{height:.2f}',
                    xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3),
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=9)
    
    for bar in bars2:
        height = bar.get_height()
        ax.annotate(f'{height:.2f}',
                    xy=(bar.get_x() + bar.get_width() / 2, height),
                    xytext=(0, 3),
                    textcoords="offset points",
                    ha='center', va='bottom', fontsize=9)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\n对比图表已保存: {output_path}")
    plt.close()


if __name__ == '__main__':
    # 示例：模拟对比数据
    print("=" * 80)
    print("66大顺 V1.0 vs V1.1 对比分析工具")
    print("=" * 80)
    
    # 这里应该从实际回测结果中读取数据
    # 示例数据（需要替换为真实数据）
    v10_example = {
        'total_return': 46.90,
        'ann_return': 15.50,
        'sharpe': 3.82,
        'max_drawdown': -5.98,
        'win_rate': 65.0,
        'profit_ratio': 2.5,
        'profit_factor': 3.2,
        'total_trades': 450,
    }
    
    v11_example = {
        'total_return': 65.00,  # 预期+38%
        'ann_return': 20.50,    # 预期+32%
        'sharpe': 4.80,         # 预期+26%
        'max_drawdown': -4.50,  # 预期改善25%
        'win_rate': 72.0,       # 预期+11%
        'profit_ratio': 3.0,    # 预期+20%
        'profit_factor': 4.0,   # 预期+25%
        'total_trades': 720,    # 预期+60%
    }
    
    comparison = compare_versions(
        v10_example, v11_example,
        v10_signals=551,  # V1.0实际信号数
        v11_signals=880   # V1.1预期信号数
    )
    
    print_comparison_report(comparison)
    
    # 保存对比图表
    output_path = os.path.join("data", "output", "v1_0_vs_v1_1_comparison.png")
    plot_comparison_chart(comparison, output_path)
    
    print("\n对比分析完成")
    print("=" * 80)
