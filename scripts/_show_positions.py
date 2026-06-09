"""查看纸上交易当前持仓详情"""
import json, sqlite3
import pandas as pd

s = json.load(open("data/output/paper_trade_state.json", "r", encoding="utf-8"))
conn = sqlite3.connect("data/db/stock.db")
names = dict(pd.read_sql("SELECT code, name FROM stock_list", conn).values)
conn.close()

print("=" * 78)
print("66大顺 V2.2 纸上交易 — 当前持仓明细")
print("=" * 78)

positions = s["positions"]
total_mv = 0
total_cost = 0
total_unrealized = 0

print(f"\n{'代码':<8} {'名称':<10} {'轨道':<4} {'ML':<3} {'买入价':>8} {'现价':>8} "
      f"{'盈亏%':>8} {'市值':>10} {'持有天数'}")
print("-" * 78)

for code, p in positions.items():
    name = names.get(code, "?")
    track = "A" if p["signal_track"] == 0 else "B"
    ret = (p["last_price"] / p["entry_price"] - 1) * 100
    mv = p["shares"] * p["last_price"]
    total_mv += mv
    total_cost += p["cost_basis"]
    total_unrealized += (mv - p["cost_basis"])
    days_info = f"{p['held_days']}/{p['hold_days_target']}"
    
    # 标记状态
    if track == "A" and p["held_days"] >= p["hold_days_target"] - 1:
        days_info += " (即将到期)"
    
    print(f"{code:<8} {name:<10} {track:<4} {p['score_ml']:<3} "
          f"¥{p['entry_price']:>7.2f} ¥{p['last_price']:>7.2f} "
          f"{ret:>+7.2f}% ¥{mv:>9,.0f} {days_info}")

print("-" * 78)
print(f"{'合计':<22} {'':>16} {'':>8} "
      f"{total_unrealized/total_cost*100:>+7.2f}% ¥{total_mv:>9,.0f}")

print(f"\n现金: ¥{s['cash']:,.2f}")
print(f"总资产: ¥{s['cash'] + total_mv:,.2f}")
print(f"净值: {(s['cash'] + total_mv) / 1_000_000:.4f}")

# 按信号日分组
print(f"\n{'='*78}")
print("按买入日分组:")
print(f"{'='*78}")

from collections import defaultdict
by_date = defaultdict(list)
for code, p in positions.items():
    by_date[p["entry_date"]].append((code, p))

for date, items in sorted(by_date.items()):
    date_items_ret = []
    for code, p in items:
        ret = (p["last_price"] / p["entry_price"] - 1) * 100
        date_items_ret.append(ret)
    avg_ret = sum(date_items_ret) / len(date_items_ret)
    signal_date = items[0][1].get("signal_date", "?")
    names_str = ", ".join(f"{names.get(c,'?')}" for c, _ in items)
    print(f"  {date} (信号日{signal_date}): {len(items)}只, 平均盈亏{avg_ret:+.2f}%")
    print(f"    {names_str}")

# 待执行挂单
pending = s.get("pending_orders", [])
if pending:
    print(f"\n{'='*78}")
    print(f"待执行挂单: {len(pending)}笔")
    print(f"{'='*78}")
    for o in pending:
        track = "A" if o["signal_track"] == 0 else "B"
        name = names.get(o["code"], "?")
        print(f"  [{track}] {o['code']} {name} ML={o['score_ml']} (信号日{o['signal_date']})")

# 净值走势
print(f"\n{'='*78}")
print("每日净值:")
print(f"{'='*78}")
for rec in s["daily_nav"]:
    ret = (rec["nav"] - 1) * 100
    print(f"  {rec['date']}: ¥{rec['total_value']:>12,.2f} 净值{rec['nav']:.4f} ({ret:+.2f}%) "
          f"持仓{rec['positions_count']}只")
