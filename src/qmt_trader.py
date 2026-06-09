"""66大顺 V2.2 QMT 实盘交易核心引擎

功能：
  - 连接 miniQMT 客户端，通过 xtquant SDK 执行真实交易
  - 管理本地状态文件（信号元数据：轨道/ML评分/持有天数）
  - 同步券商持仓与本地状态
  - 风控检查（日亏损熔断/单股止损/仓位上限）
  - 支持 DRY_RUN 模拟模式（不实际下单，仅打印日志）

设计原则：
  - 信号生成逻辑完全复用 run_paper_trade.py
  - 仅替换执行层（paper_trader → qmt_trader）
  - 本地状态记录策略元数据，券商负责资金和持仓

Version: 1.0
Date: 2026-06-09
"""

import json
import os
import csv
import time
from datetime import datetime
from collections import defaultdict

import numpy as np

from config.qmt_config import (
    QMT_PATH, ACCOUNT_ID, ACCOUNT_TYPE, SESSION_ID,
    DRY_RUN,
    DAILY_LOSS_LIMIT, SINGLE_STOCK_MAX_LOSS,
    MAX_TOTAL_POSITION_PCT, MIN_CASH_RESERVE,
    COMMISSION_RATE, COMMISSION_MIN, STAMP_DUTY_RATE,
    BUY_PRICE_BUFFER, SELL_PRICE_BUFFER,
    LIMIT_UP_THRESHOLD,
    LIVE_STATE_FILE, LIVE_TRADE_LOG,
)
from scripts.v2_0_execution import (
    V2_0_CONFIG,
    check_track_b_exit,
    get_track_hold_days,
    get_track_position_pct,
    get_market_multiplier,
)

# ── 每日信号上限（与 paper_trader 一致） ──
DAILY_LIMITS = {
    'strong': {'a': 4, 'b': 3},
    'choppy': {'a': 3, 'b': 2},
}

# ── xtquant 延迟导入 ──
_xt_trader = None
_xt_data = None

def _import_xtquant():
    """延迟导入 xtquant，DRY_RUN 模式下不强制要求安装"""
    global _xt_trader, _xt_data
    if _xt_trader is not None:
        return True
    try:
        import xtquant.xttrader as xttrader
        import xtquant.xtdata as xtdata
        _xt_trader = xttrader
        _xt_data = xtdata
        return True
    except ImportError:
        if not DRY_RUN:
            raise ImportError(
                "xtquant 未安装。请将 miniQMT 安装目录下的 xtquant 包复制到 Python 环境中，"
                "或设置 DRY_RUN=True 进行模拟测试。"
            )
        print("[QmtTrader] xtquant 未安装，当前为 DRY_RUN 模拟模式")
        return False


def _code_to_qmt(code):
    """股票代码 → QMT格式 (600xxx.SH / 000xxx.SZ / 300xxx.SZ / 688xxx.SH)"""
    code = str(code).zfill(6)
    if code.startswith(('6', '9')):
        return f"{code}.SH"
    else:
        return f"{code}.SZ"


def _qmt_to_code(qmt_code):
    """QMT格式 → 纯6位代码"""
    return qmt_code.split('.')[0]


def _round2(val):
    return round(val, 2)


def _round4(val):
    return round(val, 4)


class QmtTrader:
    """V2.2 QMT 实盘交易引擎

    接口设计对齐 PaperTrader，run_live_trade.py 可无缝切换。
    """

    def __init__(self, state_file=None):
        self.state_file = state_file or LIVE_STATE_FILE
        self.state = None
        self.trader = None
        self.account = None
        self._connected = False
        self._load_or_init()
        _import_xtquant()

    # ── 连接管理 ──

    def connect(self):
        """连接 miniQMT 客户端"""
        if DRY_RUN:
            print("[QmtTrader] DRY_RUN 模式，跳过连接")
            self._connected = True
            return True

        if not _import_xtquant():
            return False

        if self._connected:
            return True

        if not ACCOUNT_ID:
            print("[QmtTrader] 错误: 请在 config/qmt_config.py 中填写 ACCOUNT_ID")
            return False

        try:
            self.trader = _xt_trader.XtQuantTrader(QMT_PATH, SESSION_ID)
            self.trader.start()

            connect_result = self.trader.connect()
            if connect_result != 0:
                print(f"[QmtTrader] 连接失败，错误码: {connect_result}")
                return False

            # 创建账户对象
            account_type = (_xt_trader.CREDIT_ACCOUNT
                          if ACCOUNT_TYPE == "CREDIT"
                          else _xt_trader.STOCK_ACCOUNT)
            self.account = _xt_trader.StockAccount(ACCOUNT_ID, account_type)

            # 订阅账户
            subscribe_result = self.trader.subscribe(self.account)
            if subscribe_result != 0:
                print(f"[QmtTrader] 账户订阅失败: {subscribe_result}")
                return False

            self._connected = True
            print(f"[QmtTrader] 连接成功: {ACCOUNT_ID} ({ACCOUNT_TYPE})")
            return True

        except Exception as e:
            print(f"[QmtTrader] 连接异常: {e}")
            return False

    def disconnect(self):
        """断开连接"""
        if self.trader:
            try:
                self.trader.stop()
            except Exception:
                pass
        self._connected = False
        print("[QmtTrader] 已断开")

    # ── 状态持久化 ──

    def _load_or_init(self):
        """加载或初始化本地状态"""
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.state = json.load(f)
            print(f"[QmtTrader] 加载状态: {self.state_file}")
            print(f"  持仓记录: {len(self.state.get('positions', {}))}只")
        else:
            self.state = {
                "positions": {},          # 本地策略元数据
                "pending_orders": [],     # 待执行挂单
                "trade_history": [],      # 已完成交易
                "daily_nav": [],          # 每日净值
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "last_update": None,
                "start_date": None,
            }
            print(f"[QmtTrader] 初始化新状态文件")

    def save(self):
        """持久化本地状态"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    # ── 券商数据查询 ──

    def query_cash(self):
        """查询券商账户可用资金"""
        if DRY_RUN:
            # DRY_RUN 模式从本地状态估算
            return self.state.get("_dry_run_cash", 1_000_000)

        if not self._connected:
            return 0
        try:
            asset = self.trader.query_stock_asset(self.account)
            if asset:
                return float(asset.cash)
        except Exception as e:
            print(f"[QmtTrader] 查询资金失败: {e}")
        return 0

    def query_positions(self):
        """查询券商持仓 → {code: {'shares': N, 'avg_price': X, 'market_value': V}}"""
        if DRY_RUN:
            return {}

        if not self._connected:
            return {}
        try:
            positions = self.trader.query_stock_positions(self.account)
            result = {}
            for pos in positions:
                code = _qmt_to_code(pos.stock_code)
                if pos.volume > 0:
                    result[code] = {
                        'shares': pos.volume,
                        'avg_price': pos.open_price,
                        'market_value': pos.market_value,
                        'can_use_volume': pos.can_use_volume,
                    }
            return result
        except Exception as e:
            print(f"[QmtTrader] 查询持仓失败: {e}")
            return {}

    def query_total_value(self):
        """查询券商总资产（现金 + 持仓市值）"""
        if DRY_RUN:
            return self.state.get("_dry_run_cash", 1_000_000)

        if not self._connected:
            return 0
        try:
            asset = self.trader.query_stock_asset(self.account)
            if asset:
                return float(asset.total_asset)
        except Exception as e:
            print(f"[QmtTrader] 查询总资产失败: {e}")
        return 0

    # ── 下单执行 ──

    def buy(self, code, shares, price=0):
        """买入股票

        code: 6位代码
        shares: 股数（100整数倍）
        price: 限价（0=最新价）
        返回: order_id 或 None
        """
        qmt_code = _code_to_qmt(code)

        if DRY_RUN:
            order_id = f"DRY_{int(time.time()*1000)}"
            print(f"  [DRY_RUN 买入] {qmt_code} {shares}股 "
                  f"@ ¥{price:.2f}" if price > 0 else f"  [DRY_RUN 买入] {qmt_code} {shares}股 @ 市价")
            return order_id

        if not self._connected:
            return None

        try:
            if price > 0:
                # 限价单，加 buffer 确保成交
                limit_price = round(price * (1 + BUY_PRICE_BUFFER), 2)
                order_id = self.trader.order_stock(
                    self.account, qmt_code,
                    _xt_trader.STOCK_BUY,
                    shares,
                    _xt_trader.FIX_PRICE,
                    limit_price,
                )
            else:
                # 最新价
                order_id = self.trader.order_stock(
                    self.account, qmt_code,
                    _xt_trader.STOCK_BUY,
                    shares,
                    _xt_trader.LATEST_PRICE,
                    0,
                )
            print(f"  [委托买入] {qmt_code} {shares}股 → 委托号: {order_id}")
            return order_id

        except Exception as e:
            print(f"  [买入失败] {qmt_code}: {e}")
            return None

    def sell(self, code, shares, price=0):
        """卖出股票

        code: 6位代码
        shares: 股数
        price: 限价（0=最新价）
        返回: order_id 或 None
        """
        qmt_code = _code_to_qmt(code)

        if DRY_RUN:
            order_id = f"DRY_{int(time.time()*1000)}"
            print(f"  [DRY_RUN 卖出] {qmt_code} {shares}股 "
                  f"@ ¥{price:.2f}" if price > 0 else f"  [DRY_RUN 卖出] {qmt_code} {shares}股 @ 市价")
            return order_id

        if not self._connected:
            return None

        try:
            if price > 0:
                limit_price = round(price * (1 - SELL_PRICE_BUFFER), 2)
                order_id = self.trader.order_stock(
                    self.account, qmt_code,
                    _xt_trader.STOCK_SELL,
                    shares,
                    _xt_trader.FIX_PRICE,
                    limit_price,
                )
            else:
                order_id = self.trader.order_stock(
                    self.account, qmt_code,
                    _xt_trader.STOCK_SELL,
                    shares,
                    _xt_trader.LATEST_PRICE,
                    0,
                )
            print(f"  [委托卖出] {qmt_code} {shares}股 → 委托号: {order_id}")
            return order_id

        except Exception as e:
            print(f"  [卖出失败] {qmt_code}: {e}")
            return None

    # ── 风控检查 ──

    def check_daily_loss(self, current_total_value):
        """检查当日亏损是否触发熔断

        返回: (is_safe, message)
        """
        if not self.state.get("daily_nav"):
            return True, "无历史净值数据"

        last_nav = self.state["daily_nav"][-1].get("total_value", current_total_value)
        if last_nav <= 0:
            return True, "净值异常"

        daily_return = (current_total_value / last_nav - 1)
        if daily_return <= DAILY_LOSS_LIMIT:
            msg = (f"⚠️ 日亏损熔断触发! 当日亏损{daily_return*100:+.2f}% "
                   f"(阈值{DAILY_LOSS_LIMIT*100:.1f}%)，暂停所有买入")
            print(f"  [风控] {msg}")
            return False, msg
        return True, f"日亏损正常 ({daily_return*100:+.2f}%)"

    def check_single_stock_loss(self, code, current_price):
        """检查单股亏损是否触发强制平仓

        返回: (need_sell, message)
        """
        pos = self.state.get("positions", {}).get(code)
        if not pos:
            return False, "无持仓记录"

        entry_price = pos.get("entry_price", 0)
        if entry_price <= 0:
            return False, "无入场价"

        ret = (current_price / entry_price - 1)
        if ret <= SINGLE_STOCK_MAX_LOSS:
            msg = (f"⚠️ 单股止损触发! {code} {pos.get('name','')} "
                   f"亏损{ret*100:+.2f}% (阈值{SINGLE_STOCK_MAX_LOSS*100:.1f}%)")
            print(f"  [风控] {msg}")
            return True, msg
        return False, f"{code} 盈亏{ret*100:+.2f}%"

    def check_position_limit(self, current_total_value, buy_amount):
        """检查买入后是否超过仓位上限

        返回: (is_allowed, message)
        """
        cash = self.query_cash()
        if cash < MIN_CASH_RESERVE:
            return False, f"可用资金 ¥{cash:,.0f} < 最低保留 ¥{MIN_CASH_RESERVE:,.0f}"

        current_positions_value = current_total_value - cash
        new_positions_value = current_positions_value + buy_amount
        position_pct = new_positions_value / current_total_value if current_total_value > 0 else 0

        if position_pct > MAX_TOTAL_POSITION_PCT:
            msg = (f"仓位超限: 买入后仓位{position_pct*100:.1f}% "
                   f"> 上限{MAX_TOTAL_POSITION_PCT*100:.0f}%")
            print(f"  [风控] {msg}")
            return False, msg
        return True, f"仓位正常 ({position_pct*100:.1f}%)"

    # ── 信号 → 挂单（与 PaperTrader 接口一致） ──

    def generate_orders(self, signals, signal_date, market_state):
        """根据V2.2信号生成次日挂单"""
        if not signals:
            return 0

        track_a = sorted(
            [s for s in signals if s.get("signal_track") == 0],
            key=lambda x: x.get("score_ml", 0), reverse=True
        )
        track_b = sorted(
            [s for s in signals if s.get("signal_track") == 1],
            key=lambda x: x.get("score_ml", 0), reverse=True
        )

        limits = DAILY_LIMITS.get(market_state, {'a': 0, 'b': 0})
        track_a = track_a[:limits['a']]
        track_b = track_b[:limits['b']]

        orders = []
        for sig in track_a + track_b:
            code = sig["code"]
            if code in self.state.get("positions", {}):
                continue
            if any(o["code"] == code for o in self.state.get("pending_orders", [])):
                continue

            track = sig.get("signal_track", 0)
            mult = get_market_multiplier(track, market_state)
            if mult <= 0:
                continue

            orders.append({
                "code": code,
                "name": sig.get("name", ""),
                "signal_date": signal_date,
                "signal_track": track,
                "score_ml": sig.get("score_ml", 0),
                "market_state": market_state,
                "mult": mult,
            })

        self.state["pending_orders"] = orders
        if orders:
            mode = "DRY_RUN" if DRY_RUN else "实盘"
            print(f"[QmtTrader] 生成{len(orders)}笔挂单 ({mode}, 信号日{signal_date}):")
            for o in orders:
                track_name = "A" if o["signal_track"] == 0 else "B"
                print(f"  [{track_name}] {o['code']} {o['name']} "
                      f"ML={o['score_ml']} 市况={o['market_state']}")
        return len(orders)

    # ── 执行挂单（次日开盘） ──

    def execute_pending_orders(self, open_price_map, exec_date,
                                close_price_map=None, price_map_today=None):
        """执行前一日挂单，通过 QMT 真实下单买入

        open_price_map: {code: open_price} 当日开盘价
        exec_date: 执行日期
        close_price_map: {code: close_price} 信号日收盘价（涨停检测用）
        price_map_today: {code: price} 当前最新价（下单参考价）
        """
        orders = self.state.get("pending_orders", [])
        if not orders:
            return 0

        # ── 风控: 日亏损熔断 ──
        total_value = self.query_total_value()
        is_safe, msg = self.check_daily_loss(total_value)
        if not is_safe:
            print(f"  [熔断] 暂停所有买入: {msg}")
            self.state["pending_orders"] = []
            return 0

        executed = 0
        skipped_limit = 0

        for order in orders:
            code = order["code"]
            open_price = open_price_map.get(code)
            if open_price is None or open_price <= 0:
                print(f"  [跳过] {code} {order['name']}: 无开盘价")
                continue

            # 涨停检测
            if close_price_map and code in close_price_map:
                signal_close = close_price_map[code]
                if signal_close > 0:
                    gap_pct = (open_price / signal_close - 1)
                    if gap_pct >= LIMIT_UP_THRESHOLD:
                        print(f"  [跳过] {code} {order['name']}: "
                              f"涨停开盘 +{gap_pct*100:.1f}%")
                        skipped_limit += 1
                        continue

            # 仓位计算
            track = order["signal_track"]
            pos_pct = get_track_position_pct(track)
            pos_size = total_value * pos_pct * order["mult"]

            # 风控: 仓位上限
            is_allowed, msg = self.check_position_limit(total_value, pos_size)
            if not is_allowed:
                print(f"  [跳过] {code} {order['name']}: {msg}")
                continue

            # 计算股数
            buy_price = open_price  # 实盘以开盘价委托
            shares = int(pos_size / buy_price / 100) * 100
            if shares < 100:
                continue

            # 风控: 现金保留
            cash = self.query_cash()
            estimated_cost = shares * buy_price * (1 + COMMISSION_RATE)
            if estimated_cost > cash - MIN_CASH_RESERVE:
                shares = int((cash - MIN_CASH_RESERVE) / buy_price / 100) * 100
                if shares < 100:
                    print(f"  [跳过] {code} {order['name']}: 资金不足")
                    continue

            # ── 下单 ──
            actual_price = price_map_today.get(code, open_price) if price_map_today else open_price
            order_id = self.buy(code, shares, price=actual_price)
            if order_id is None:
                continue

            # ML分档持有天数
            ml_score = order["score_ml"]
            hold_days = get_track_hold_days(ml_score, track)

            # 记录本地状态
            self.state["positions"][code] = {
                "name": order["name"],
                "entry_date": exec_date,
                "entry_price": _round4(buy_price),
                "shares": shares,
                "order_id": str(order_id),
                "signal_track": track,
                "score_ml": ml_score,
                "hold_days_target": hold_days,
                "held_days": 0,
                "peak_price": _round4(buy_price),
                "signal_date": order["signal_date"],
                "market_state": order["market_state"],
            }
            executed += 1
            track_name = "A" if track == 0 else "B"
            print(f"  [买入] [{track_name}] {code} {order['name']} "
                  f"{shares}股 @ ¥{buy_price:.2f} (委托号: {order_id})")

        self.state["pending_orders"] = []
        if skipped_limit:
            print(f"  [统计] 执行{executed}笔, 涨停跳过{skipped_limit}笔")
        return executed

    # ── 检查持仓退出 ──

    def check_exits(self, price_map, current_date):
        """检查所有持仓是否需要退出（逻辑与 PaperTrader 一致）"""
        to_sell = []

        for code, pos in list(self.state.get("positions", {}).items()):
            price = price_map.get(code)
            if price is None or price <= 0:
                continue

            pos["held_days"] += 1
            if price > pos.get("peak_price", pos["entry_price"]):
                pos["peak_price"] = _round4(price)

            # 风控: 单股止损
            need_force_sell, msg = self.check_single_stock_loss(code, price)
            if need_force_sell:
                to_sell.append((code, pos, price, "RISK_SINGLE_STOP"))
                continue

            track = pos.get("signal_track", 0)
            exit_reason = None

            if track == 0:
                if pos["held_days"] >= pos.get("hold_days_target", 7):
                    exit_reason = "A_HOLD_EXPIRY"
            elif track == 1:
                exit_reason = check_track_b_exit(pos, price)
                if exit_reason is None:
                    if pos["held_days"] >= V2_0_CONFIG["track_b"]["max_hold_days"]:
                        exit_reason = "B_HOLD_EXPIRY"

            if exit_reason:
                to_sell.append((code, pos, price, exit_reason))

        return to_sell

    def execute_sells(self, sells, current_date, price_map_today=None):
        """通过 QMT 执行卖出

        sells: list of (code, pos, price, exit_reason)
        price_map_today: {code: price} 当前最新价（下单参考价）
        """
        closed = []
        for code, pos, sell_price, reason in sells:
            shares = pos["shares"]

            # 下单
            actual_price = price_map_today.get(code, sell_price) if price_map_today else sell_price
            order_id = self.sell(code, shares, price=actual_price)
            if order_id is None:
                print(f"  [卖出失败] {code} {pos.get('name','')}")
                continue

            # 盈亏计算（以策略价估算）
            entry_price = pos["entry_price"]
            return_pct = (sell_price / entry_price - 1) * 100
            gross_proceeds = shares * sell_price
            commission = max(gross_proceeds * COMMISSION_RATE, COMMISSION_MIN)
            stamp_duty = gross_proceeds * STAMP_DUTY_RATE
            pnl = gross_proceeds - shares * entry_price - commission - stamp_duty

            track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
            print(f"  [卖出] [{track_name}] {code} {pos['name']} "
                  f"{shares}股 @ ¥{sell_price:.2f} "
                  f"收益={return_pct:+.2f}% PnL≈¥{pnl:+,.0f} "
                  f"({reason}) 委托号:{order_id}")

            trade_record = {
                "code": code,
                "name": pos.get("name", ""),
                "signal_date": pos.get("signal_date", ""),
                "entry_date": pos["entry_date"],
                "exit_date": current_date,
                "entry_price": entry_price,
                "exit_price": _round4(sell_price),
                "shares": shares,
                "signal_track": pos.get("signal_track", 0),
                "score_ml": pos.get("score_ml", 0),
                "return_pct": _round2(return_pct),
                "pnl": _round2(pnl),
                "exit_reason": reason,
                "held_days": pos.get("held_days", 0),
                "order_id_sell": str(order_id),
            }
            closed.append(trade_record)
            self.state.setdefault("trade_history", []).append(trade_record)

            # 删除本地持仓记录
            del self.state["positions"][code]

        return closed

    # ── 每日净值记录 ──

    def record_daily_nav(self, current_date, price_map):
        """记录当日收盘后净值（从券商查询真实资产）"""
        total_value = self.query_total_value()
        cash = self.query_cash()

        # DRY_RUN 模式用本地估算
        if DRY_RUN:
            positions_value = 0
            for code, pos in self.state.get("positions", {}).items():
                price = price_map.get(code, pos.get("entry_price", 0))
                positions_value += pos.get("shares", 0) * price
            cash = self.state.get("_dry_run_cash", 1_000_000)
            total_value = cash + positions_value

        record = {
            "date": current_date,
            "cash": _round2(cash),
            "positions_value": _round2(total_value - cash),
            "total_value": _round2(total_value),
            "nav": _round4(total_value / 1_000_000),  # 以100万为基准
            "positions_count": len(self.state.get("positions", {})),
        }
        self.state.setdefault("daily_nav", []).append(record)
        self.state["last_update"] = current_date

        if self.state.get("start_date") is None:
            self.state["start_date"] = current_date

        return record

    # ── 打印状态 ──

    def print_status(self, price_map=None):
        """打印当前状态"""
        positions = self.state.get("positions", {})
        pending = len(self.state.get("pending_orders", []))
        mode = "DRY_RUN" if DRY_RUN else "实盘"

        total_value = self.query_total_value()
        cash = self.query_cash()

        if DRY_RUN:
            positions_value = 0
            for code, pos in positions.items():
                price = pos.get("entry_price", 0)
                if price_map and code in price_map:
                    price = price_map[code]
                positions_value += pos.get("shares", 0) * price
            cash = self.state.get("_dry_run_cash", 1_000_000)
            total_value = cash + positions_value

        print(f"\n{'='*60}")
        print(f"66大顺 V2.2 实盘交易 — 当前状态 [{mode}]")
        print(f"{'='*60}")
        print(f"  账户:       {ACCOUNT_ID or '未配置'}")
        print(f"  总资产:     ¥{total_value:>14,.2f}")
        print(f"  可用现金:   ¥{cash:>14,.2f}")
        print(f"  持仓数量:   {len(positions)}只")
        if pending:
            print(f"  挂单中:     {pending}笔（待次日执行）")

        if positions:
            print(f"\n  当前持仓:")
            print(f"  {'代码':<8} {'名称':<8} {'轨道':<4} {'天数':<4} "
                  f"{'盈亏':>8} {'市值':>12}")
            print(f"  {'-'*52}")
            for code, pos in positions.items():
                price = pos.get("entry_price", 0)
                if price_map and code in price_map:
                    price = price_map[code]
                ret = (price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0
                mv = pos.get("shares", 0) * price
                track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
                print(f"  {code:<8} {pos.get('name',''):<8} {track_name:<4} "
                      f"{pos.get('held_days',0):<4} {ret:>+7.2f}% ¥{mv:>11,.0f}")
        print(f"{'='*60}")

    # ── 日报 ──

    def build_daily_report(self, current_date):
        """构建每日交易报告（格式与 PaperTrader 一致）"""
        fmt_date = f"{current_date[:4]}-{current_date[4:6]}-{current_date[6:8]}"
        mode = "DRY_RUN" if DRY_RUN else "实盘"
        lines = []
        sep = "=" * 60
        thin = "-" * 60

        total_value = self.query_total_value()
        cash = self.query_cash()
        if DRY_RUN:
            positions_value = 0
            for code, pos in self.state.get("positions", {}).items():
                positions_value += pos.get("shares", 0) * pos.get("entry_price", 0)
            cash = self.state.get("_dry_run_cash", 1_000_000)
            total_value = cash + positions_value

        nav = total_value / 1_000_000
        ret_pct = (nav - 1) * 100

        lines.append(sep)
        lines.append(f"  66大顺 V2.2 实盘日报 ({fmt_date}) [{mode}]")
        lines.append(sep)
        lines.append("")

        # 账户概览
        lines.append("【账户概览】")
        lines.append(f"  账户:       {ACCOUNT_ID or '未配置'}")
        lines.append(f"  总资产:     ¥{total_value:>12,.2f}")
        lines.append(f"  可用现金:   ¥{cash:>12,.2f}")
        lines.append(f"  持仓市值:   ¥{total_value - cash:>12,.2f}")
        lines.append(f"  累计收益:   {ret_pct:>+11.2f}%")
        lines.append(f"  净值:       {nav:>12.4f}")
        lines.append("")

        # 当日买入
        today_buys = []
        for code, pos in self.state.get("positions", {}).items():
            if pos.get("entry_date") == current_date:
                today_buys.append((code, pos))

        lines.append("【当日买入】")
        if today_buys:
            for code, pos in today_buys:
                track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
                lines.append(
                    f"  [{track_name}] {code} {pos.get('name','')} "
                    f"{pos.get('shares',0)}股 @ ¥{pos.get('entry_price',0):.2f}"
                )
        else:
            lines.append("  今日无买入")
        lines.append("")

        # 当日卖出
        today_sells = [t for t in self.state.get("trade_history", [])
                       if t.get("exit_date") == current_date]
        lines.append("【当日卖出】")
        if today_sells:
            for t in today_sells:
                track_name = "A" if t.get("signal_track", 0) == 0 else "B"
                lines.append(
                    f"  [{track_name}] {t['code']} {t['name']} "
                    f"{t['shares']}股 @ ¥{t['exit_price']:.2f} "
                    f"{t['return_pct']:+.2f}% ({t['exit_reason']})"
                )
            total_pnl = sum(t["pnl"] for t in today_sells)
            lines.append(f"  卖出合计: {len(today_sells)}笔, 盈亏≈¥{total_pnl:+,.0f}")
        else:
            lines.append("  今日无卖出")
        lines.append("")

        # 当前持仓
        positions = self.state.get("positions", {})
        lines.append("【当前持仓】")
        if positions:
            for code, pos in positions.items():
                price = pos.get("entry_price", 0)
                ret = (price / pos["entry_price"] - 1) * 100 if pos["entry_price"] > 0 else 0
                mv = pos.get("shares", 0) * price
                track_name = "A" if pos.get("signal_track", 0) == 0 else "B"
                lines.append(
                    f"  [{track_name}] {code} {pos.get('name','')} "
                    f"{pos.get('held_days',0)}天 "
                    f"¥{pos.get('entry_price',0):.2f}→¥{price:.2f} "
                    f"{ret:+.2f}% ¥{mv:,.0f}"
                )
        else:
            lines.append("  当前无持仓")
        lines.append("")

        # 挂单
        pending = self.state.get("pending_orders", [])
        if pending:
            lines.append("【待执行挂单（次日开盘买入）】")
            for o in pending:
                track_name = "A" if o.get("signal_track", 0) == 0 else "B"
                lines.append(f"  [{track_name}] {o['code']} {o['name']}  "
                             f"ML={o.get('score_ml', 0)}")

        lines.append("")
        lines.append(sep)
        lines.append(f"  报告时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(sep)
        return "\n".join(lines)

    # ── 交易日志 CSV ──

    def append_trade_log_csv(self):
        """将交易历史写入 CSV 日志"""
        trades = self.state.get("trade_history", [])
        if not trades:
            return

        os.makedirs(os.path.dirname(LIVE_TRADE_LOG), exist_ok=True)
        fieldnames = [
            "code", "name", "signal_date", "entry_date", "exit_date",
            "entry_price", "exit_price", "shares", "signal_track",
            "score_ml", "return_pct", "pnl", "exit_reason", "held_days",
            "order_id_sell",
        ]
        with open(LIVE_TRADE_LOG, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in trades:
                writer.writerow({k: t.get(k, "") for k in fieldnames})
        print(f"[QmtTrader] 交易日志已保存: {LIVE_TRADE_LOG}")

    # ── 同步券商持仓到本地 ──

    def sync_with_broker(self):
        """同步券商持仓与本地状态

        - 券商有但本地无 → 手动买入的，加入本地（标记为 manual）
        - 本地有但券商无 → 已卖出/未成交，从本地删除
        """
        if DRY_RUN:
            print("[QmtTrader] DRY_RUN 模式，跳过券商同步")
            return

        broker_positions = self.query_positions()
        local_positions = self.state.get("positions", {})

        # 券商有但本地无
        for code, info in broker_positions.items():
            if code not in local_positions:
                local_positions[code] = {
                    "name": "",
                    "entry_date": datetime.now().strftime("%Y%m%d"),
                    "entry_price": info.get("avg_price", 0),
                    "shares": info["shares"],
                    "signal_track": 0,
                    "score_ml": 0,
                    "hold_days_target": 999,
                    "held_days": 0,
                    "peak_price": info.get("avg_price", 0),
                    "signal_date": "",
                    "market_state": "",
                    "_source": "broker_manual",
                }
                print(f"  [同步] 新增券商持仓: {code} {info['shares']}股 "
                      f"@ ¥{info.get('avg_price', 0):.2f}")

        # 本地有但券商无
        to_remove = []
        for code in local_positions:
            if code not in broker_positions:
                source = local_positions[code].get("_source", "")
                if source != "broker_manual":
                    # 策略单可能还在成交中，不立即删除
                    pass
                else:
                    to_remove.append(code)

        for code in to_remove:
            del local_positions[code]
            print(f"  [同步] 移除已清仓: {code}")

        self.state["positions"] = local_positions
