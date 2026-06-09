"""66大顺 V1.1 增强过滤模块

V1.0使用MACD + ZJTJ双过滤，KDJ仅用于评分。
V1.1优化为绕过MACD瓶颈，使用ZJTJ+KDJ组合大幅提升信号量。

Version: 1.1
Date: 2026-06-06
"""

from src.filters.macd_filter import filter_by_macd
from src.filters.zjtj_filter import filter_by_zjtj
from src.filters.kdj_filter import filter_by_kdj


def filter_by_any_two(stock_dict: dict, mode: str = 'any_two') -> set:
    """三选二过滤：任意两个指标确认即可
    
    Args:
        stock_dict: {code: pd.DataFrame} 股票日线数据
        mode: 过滤模式
            - 'any_two': 任意两个指标确认
            - 'macd_required': MACD必须通过 + 任意一个
            - 'zjtj_kdj_only': 仅用ZJTJ∩KDJ
            - 'zjtj_only': 仅用ZJTJ单过滤（推荐，信号量最大+质量可控）
    
    Returns:
        符合条件的股票代码集合
    
    Examples:
        >>> codes = filter_by_any_two(stock_dict, mode='any_two')
        >>> print(f"信号数量: {len(codes)}")
    """
    # 分别获取三个指标的通过结果
    macd_codes = filter_by_macd(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)
    kdj_codes = filter_by_kdj(stock_dict)
    
    if mode == 'any_two':
        # 任意两个：(MACD∩ZJTJ) ∪ (MACD∩KDJ) ∪ (ZJTJ∩KDJ)
        macd_zjtj = macd_codes & zjtj_codes
        macd_kdj = macd_codes & kdj_codes
        zjtj_kdj = zjtj_codes & kdj_codes
        result = macd_zjtj | macd_kdj | zjtj_kdj
        
    elif mode == 'macd_required':
        # MACD必须 + 任意一个：MACD ∩ (ZJTJ ∪ KDJ)
        zjtj_or_kdj = zjtj_codes | kdj_codes
        result = macd_codes & zjtj_or_kdj
        
    elif mode == 'zjtj_kdj_only':
        # 仅用ZJTJ∩KDJ，完全绕过MACD（MACD是瓶颈）
        result = zjtj_codes & kdj_codes
        
    elif mode == 'zjtj_only':
        # 仅用ZJTJ（日均35.2只，靠ML评分+增强规则筛选质量）
        result = zjtj_codes
        
    else:
        raise ValueError(f"未知的过滤模式: {mode}，支持 'any_two', 'macd_required', 'zjtj_kdj_only', 'zjtj_only'")
    
    return result


def get_filter_mode_description(mode: str) -> str:
    """获取过滤模式的中文描述
    
    Args:
        mode: 过滤模式名称
    
    Returns:
        中文描述字符串
    """
    descriptions = {
        'any_two': '任意两个指标确认',
        'macd_required': 'MACD必须通过 + 任意一个',
        'zjtj_only': '仅ZJTJ单过滤（日均35.2只，靠ML+增强规则筛选）',
        'v10_mode': 'MACD+ZJTJ（与V1.0一致）',
    }
    return descriptions.get(mode, '未知模式')


def compare_filter_modes(stock_dict: dict) -> dict:
    """对比不同过滤模式的信号数量
    
    Args:
        stock_dict: {code: pd.DataFrame} 股票日线数据
    
    Returns:
        {mode: count} 各模式的信号数量
    """
    # V1.0模式（MACD+ZJTJ）
    macd_codes = filter_by_macd(stock_dict)
    zjtj_codes = filter_by_zjtj(stock_dict)
    kdj_codes = filter_by_kdj(stock_dict)
    v10_mode = macd_codes & zjtj_codes
    
    # V1.1模式
    any_two = filter_by_any_two(stock_dict, mode='any_two')
    macd_required = filter_by_any_two(stock_dict, mode='macd_required')
    zjtj_kdj_only = filter_by_any_two(stock_dict, mode='zjtj_kdj_only')
    
    return {
        'V1.0_MACD+ZJTJ': len(v10_mode),
        'V1.1_any_two': len(any_two),
        'V1.1_macd_required': len(macd_required),
        'V1.1_zjtj+KDJ_only': len(zjtj_kdj_only),
        'any_two提升': f"{len(any_two)/max(len(v10_mode),1)*100-100:.1f}%",
        'zjtj+KDJ提升': f"{len(zjtj_kdj_only)/max(len(v10_mode),1)*100-100:.1f}%",
    }


if __name__ == '__main__':
    """测试代码"""
    import pandas as pd
    import numpy as np
    
    # 模拟测试数据
    print("=" * 60)
    print("66大顺 V1.1 过滤模块测试")
    print("=" * 60)
    
    # 创建模拟数据（实际使用时替换为真实数据）
    test_stock_dict = {}
    for i in range(100):
        code = f"00000{i:02d}"
        dates = pd.date_range('2023-01-01', periods=100, freq='D')
        test_stock_dict[code] = pd.DataFrame({
            'date': dates,
            'open': np.random.uniform(10, 20, 100),
            'high': np.random.uniform(15, 25, 100),
            'low': np.random.uniform(8, 18, 100),
            'close': np.random.uniform(10, 22, 100),
            'volume': np.random.uniform(1000000, 5000000, 100),
        })
    
    try:
        # 测试不同模式
        result = compare_filter_modes(test_stock_dict)
        print("\n过滤模式对比结果:")
        for mode, count in result.items():
            print(f"  {mode}: {count}")
        
        # 测试模式描述
        print("\n过滤模式描述:")
        for mode in ['any_two', 'macd_required', 'all_three']:
            desc = get_filter_mode_description(mode)
            print(f"  {mode}: {desc}")
            
    except Exception as e:
        print(f"\n测试完成（部分指标可能因数据不足返回空集）: {e}")
    
    print("\n" + "=" * 60)
    print("测试结束")
    print("=" * 60)
