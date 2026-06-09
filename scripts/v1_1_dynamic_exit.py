"""66大顺 V1.1 移动止盈止损模块

V1.0使用固定持有期（3/5/7天），无动态止损。
V1.1引入移动止盈止损策略，控制回撤并保护利润。

Version: 1.1
Date: 2026-06-06
"""


class DynamicExitManager:
    """动态退出管理器
    
    管理持仓的止损止盈逻辑，包括：
    - 硬止损：亏损达到阈值立即出场
    - 移动止盈：盈利激活后，从最高点回撤出场
    - 时间到期：持有期满出场
    
    Attributes:
        hard_stop_pct: 硬止损比例（默认-8%）
        trailing_activate_pct: 移动止盈激活阈值（默认+15%）
        trailing_stop_pct: 移动止盈回撤比例（默认-5%）
    """
    
    def __init__(self, 
                 hard_stop_pct: float = -0.08,
                 trailing_activate_pct: float = 0.15,
                 trailing_stop_pct: float = -0.05):
        """初始化动态退出管理器
        
        Args:
            hard_stop_pct: 硬止损比例（负数），默认-8%
            trailing_activate_pct: 移动止盈激活阈值，默认+15%
            trailing_stop_pct: 移动止盈回撤比例（负数），默认-5%
        """
        self.hard_stop_pct = hard_stop_pct
        self.trailing_activate_pct = trailing_activate_pct
        self.trailing_stop_pct = trailing_stop_pct
    
    def check_exit(self, 
                   entry_price: float,
                   current_price: float,
                   peak_price: float,
                   held_days: int,
                   target_hold_days: int) -> tuple:
        """检查是否应该退出
        
        Args:
            entry_price: 入场价格
            current_price: 当前价格
            peak_price: 持有期间最高价
            held_days: 已持有天数
            target_hold_days: 目标持有天数
        
        Returns:
            (should_exit: bool, exit_reason: str, exit_price: float)
            - should_exit: 是否应该退出
            - exit_reason: 退出原因
            - exit_price: 退出价格
        """
        # 计算收益率
        ret = (current_price / entry_price - 1) if entry_price > 0 else 0
        peak_ret = (peak_price / entry_price - 1) if entry_price > 0 else 0
        
        # 规则1: 硬止损（最高优先级）
        if ret <= self.hard_stop_pct:
            return True, "HARD_STOP", current_price
        
        # 规则2: 移动止盈（仅在盈利激活后）
        if peak_ret >= self.trailing_activate_pct:
            # 从最高点回撤
            drawdown_from_peak = (current_price / peak_price - 1)
            if drawdown_from_peak <= self.trailing_stop_pct:
                return True, "TRAILING_STOP", current_price
        
        # 规则3: 时间到期
        if held_days >= target_hold_days:
            return True, "TIME_EXIT", current_price
        
        # 继续持有
        return False, None, None
    
    def get_exit_reason_description(self, exit_reason: str) -> str:
        """获取退出原因的中文描述
        
        Args:
            exit_reason: 退出原因代码
        
        Returns:
            中文描述
        """
        descriptions = {
            'HARD_STOP': f'硬止损（亏损≥{abs(self.hard_stop_pct)*100:.0f}%）',
            'TRAILING_STOP': f'移动止盈（盈利≥{self.trailing_activate_pct*100:.0f}%后回撤≥{abs(self.trailing_stop_pct)*100:.0f}%）',
            'TIME_EXIT': '持有到期',
            'PARTIAL_TP': '部分止盈',
            'NO_DATA': '数据缺失强制平仓',
            'FORCED_CLOSE': '回测结束强制平仓',
        }
        return descriptions.get(exit_reason, '未知原因')


def create_dynamic_exit_manager(config: dict = None) -> DynamicExitManager:
    """创建动态退出管理器（工厂函数）
    
    Args:
        config: 配置字典，可选
            - hard_stop_pct: 硬止损比例
            - trailing_activate_pct: 移动止盈激活阈值
            - trailing_stop_pct: 移动止盈回撤比例
    
    Returns:
        DynamicExitManager实例
    """
    if config is None:
        return DynamicExitManager()
    
    return DynamicExitManager(
        hard_stop_pct=config.get('hard_stop_pct', -0.08),
        trailing_activate_pct=config.get('trailing_activate_pct', 0.15),
        trailing_stop_pct=config.get('trailing_stop_pct', -0.05),
    )


def test_dynamic_exit():
    """测试动态退出逻辑"""
    print("=" * 60)
    print("66大顺 V1.1 动态退出模块测试")
    print("=" * 60)
    
    manager = DynamicExitManager(
        hard_stop_pct=-0.08,
        trailing_activate_pct=0.15,
        trailing_stop_pct=-0.05
    )
    
    # 测试场景1: 硬止损触发
    print("\n场景1: 硬止损触发")
    entry = 100.0
    current = 92.0  # -8%
    peak = 100.0
    should_exit, reason, price = manager.check_exit(entry, current, peak, 3, 7)
    print(f"  入场价: {entry}, 当前价: {current}, 最高价: {peak}")
    print(f"  收益率: {(current/entry-1)*100:.2f}%")
    print(f"  是否退出: {should_exit}, 原因: {manager.get_exit_reason_description(reason)}")
    
    # 测试场景2: 移动止盈触发
    print("\n场景2: 移动止盈触发")
    entry = 100.0
    current = 108.0  # 从120回撤到108（-10%）
    peak = 120.0  # 曾盈利+20%
    should_exit, reason, price = manager.check_exit(entry, current, peak, 5, 7)
    print(f"  入场价: {entry}, 当前价: {current}, 最高价: {peak}")
    print(f"  最高收益率: {(peak/entry-1)*100:.2f}%, 当前收益率: {(current/entry-1)*100:.2f}%")
    print(f"  从 peak 回撤: {(current/peak-1)*100:.2f}%")
    print(f"  是否退出: {should_exit}, 原因: {manager.get_exit_reason_description(reason)}")
    
    # 测试场景3: 继续持有
    print("\n场景3: 继续持有")
    entry = 100.0
    current = 103.0
    peak = 105.0
    should_exit, reason, price = manager.check_exit(entry, current, peak, 3, 7)
    print(f"  入场价: {entry}, 当前价: {current}, 最高价: {peak}")
    print(f"  当前收益率: {(current/entry-1)*100:.2f}%")
    print(f"  是否退出: {should_exit}")
    
    # 测试场景4: 时间到期
    print("\n场景4: 时间到期")
    entry = 100.0
    current = 102.0
    peak = 104.0
    should_exit, reason, price = manager.check_exit(entry, current, peak, 7, 7)
    print(f"  入场价: {entry}, 当前价: {current}, 最高价: {peak}")
    print(f"  持有天数: 7, 目标天数: 7")
    print(f"  是否退出: {should_exit}, 原因: {manager.get_exit_reason_description(reason)}")
    
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == '__main__':
    test_dynamic_exit()
