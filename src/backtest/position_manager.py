"""仓位管理器 - 单笔持仓的止损止盈模拟（V2 优化版）

管理一笔买入持仓的完整生命周期：
- 三档止盈：+10%卖出20%, +20%卖出30%, +30%卖出50%
- 跟踪止损：从持仓最高点回落阈值全仓卖出（默认-12%）
- 固定硬止损：达到阈值立即平仓（不跟踪，默认-8%）
- 超期平仓：最长持仓80个交易日
- 分段止损：按total_score高低使用不同止损幅度

V2 优化点：
1. 止盈门槛从 15%/25%/50% 降至 10%/20%/30%
2. 跟踪止损从 -8% 放宽至 -12%（减少误止损）
3. 新增固定硬止损 -8%（兜底保护）
4. 持仓天数从 60 放宽至 80
5. 支持按分数分段使用不同止损幅度
"""
import logging

from config.settings import (
    TP_LEVELS as DEFAULT_TP_LEVELS,
    STOP_LOSS_DRAWDOWN as DEFAULT_SL_DRAWDOWN,
    HARD_STOP_LOSS as DEFAULT_HARD_SL,
    MAX_HOLD_DAYS as DEFAULT_MAX_HOLD,
    SL_BY_SCORE,
)

logger = logging.getLogger(__name__)


def _get_sl_by_score(total_score: int, default_drawdown: float,
                     default_hard_sl: float) -> tuple:
    """根据 total_score 获取对应的止损参数

    返回: (跟踪止损回撤阈值, 固定硬止损阈值)
    """
    for min_s, max_s, drawdown in SL_BY_SCORE:
        if min_s <= total_score <= max_s:
            return drawdown, default_hard_sl
    return default_drawdown, default_hard_sl


class PositionManager:
    """单笔持仓的止损止盈管理器

    参数:
        entry_price: 买入价格
        entry_date: 买入日期（仅用于日志）
        tp_levels: 止盈档次列表，默认使用 settings.TP_LEVELS
        sl_drawdown: 跟踪止损回撤阈值，默认使用 settings.STOP_LOSS_DRAWDOWN
        hard_sl: 固定硬止损阈值，默认使用 settings.HARD_STOP_LOSS
        max_hold_days: 最大持仓天数，默认使用 settings.MAX_HOLD_DAYS
        total_score: 综合评分，用于分段止损（-1=不启用分段）
    """

    def __init__(
        self,
        entry_price: float,
        entry_date: str = "",
        tp_levels: list = None,
        sl_drawdown: float = None,
        hard_sl: float = None,
        max_hold_days: int = None,
        total_score: int = -1,
    ):
        self.entry_price = entry_price
        self.entry_date = entry_date

        # 确定止损参数（分段优先）
        if total_score >= 0:
            dd, hs = _get_sl_by_score(total_score, DEFAULT_SL_DRAWDOWN, DEFAULT_HARD_SL)
            self.sl_drawdown = sl_drawdown if sl_drawdown is not None else dd
        else:
            self.sl_drawdown = sl_drawdown if sl_drawdown is not None else DEFAULT_SL_DRAWDOWN

        self.hard_sl = hard_sl if hard_sl is not None else DEFAULT_HARD_SL
        self.max_hold_days = max_hold_days if max_hold_days is not None else DEFAULT_MAX_HOLD
        self.tp_levels = tp_levels if tp_levels is not None else DEFAULT_TP_LEVELS

        # 仓位状态
        self.remaining_ratio = 1.0          # 剩余仓位比例 (1.0 = 100%)
        self.total_realized_return = 0.0    # 已实现总收益率 (小数,如0.15=15%)
        self.peak_return = 0.0              # 持仓期间最高收益率
        self.hold_days = 0                  # 已持仓天数
        self.is_closed = False              # 是否已清仓
        self.exit_reason = None             # 退出原因代码

        # 各档止盈触发标记
        self._tp_triggered = {th: False for th, _, _ in self.tp_levels}

    def update(self, current_price: float, date_str: str = None) -> dict:
        """更新每日价格，检查是否触发止盈/止损"""
        if self.is_closed:
            return {
                "sold_pct": 0.0,
                "added_return": 0.0,
                "is_closed": True,
                "exit_reason": self.exit_reason,
            }

        self.hold_days += 1
        current_return = (current_price / self.entry_price - 1)
        self.peak_return = max(self.peak_return, current_return)

        result = {
            "sold_pct": 0.0,
            "added_return": 0.0,
            "is_closed": False,
            "exit_reason": None,
        }

        # ── 固定硬止损检查（优先于止盈，先保本）──
        if self.remaining_ratio > 1e-10:
            if current_return <= -self.hard_sl:
                actual_sell = self.remaining_ratio
                added = actual_sell * current_return

                self.total_realized_return += added
                self.remaining_ratio = 0.0
                self.is_closed = True
                self.exit_reason = "HARD_STOP_LOSS"

                result["sold_pct"] = actual_sell
                result["added_return"] = added
                result["is_closed"] = True
                result["exit_reason"] = "HARD_STOP_LOSS"
                return result

        # ── 止盈检查 (按阈值从高到低) ──
        for threshold, sell_ratio, reason in self.tp_levels:
            if self.remaining_ratio <= 0:
                break
            if current_return >= threshold and not self._tp_triggered[threshold]:
                self._tp_triggered[threshold] = True
                actual_sell = min(sell_ratio, self.remaining_ratio)
                added = actual_sell * current_return

                self.remaining_ratio -= actual_sell
                self.total_realized_return += added

                result["sold_pct"] = actual_sell
                result["added_return"] = added
                result["exit_reason"] = reason

                if self.remaining_ratio <= 1e-10:
                    self.is_closed = True
                    self.exit_reason = reason
                    result["is_closed"] = True
                    break

        # ── 跟踪止损检查 ──
        if not self.is_closed and self.remaining_ratio > 1e-10:
            if self.hold_days >= 2:
                trailing_dd = self.peak_return - current_return
                if trailing_dd >= self.sl_drawdown:
                    actual_sell = self.remaining_ratio
                    added = actual_sell * current_return

                    self.total_realized_return += added
                    self.remaining_ratio = 0.0
                    self.is_closed = True
                    self.exit_reason = "STOP_LOSS"

                    result["sold_pct"] = actual_sell
                    result["added_return"] = added
                    result["is_closed"] = True
                    result["exit_reason"] = "STOP_LOSS"

        # ── 超期平仓 ──
        if not self.is_closed and self.remaining_ratio > 1e-10:
            if self.hold_days >= self.max_hold_days:
                actual_sell = self.remaining_ratio
                added = actual_sell * current_return

                self.total_realized_return += added
                self.remaining_ratio = 0.0
                self.is_closed = True
                self.exit_reason = "EXPIRED"

                result["sold_pct"] = actual_sell
                result["added_return"] = added
                result["is_closed"] = True
                result["exit_reason"] = "EXPIRED"

        return result

    def get_summary(self) -> dict:
        """获取持仓最终统计"""
        return {
            "total_return": round(self.total_realized_return * 100, 2),
            "hold_days": self.hold_days,
            "exit_reason": self.exit_reason or "OPEN",
            "remaining_ratio": round(self.remaining_ratio, 4),
        }
