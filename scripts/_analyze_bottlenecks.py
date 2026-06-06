"""分析当前策略的信号链路瓶颈"""
import pandas as pd
import numpy as np

df = pd.read_csv("data/output/backtest_ml_20230601_20260603.csv")

print("=== 信号链路各层损耗分析 ===")
print(f"总信号数(筛选后最终): {len(df)}")
print(f"交易日: {df['date'].nunique()}")
print(f"日均信号: {len(df)/df['date'].nunique():.1f}")

print(f"\n=== 各评分维度通过率 (score > 0 视为通过) ===")
for col in ["score_macd", "score_zjtj", "score_kdj", "score_rps", "score_volume", "score_ml"]:
    print(f"  {col}: {(df[col]>0).mean()*100:.1f}%")

print(f"\n=== ML评分分布 ===")
for lo in range(0, 16, 3):
    hi = min(lo + 2, 15)
    cnt = ((df["score_ml"] >= lo) & (df["score_ml"] <= hi)).sum()
    print(f"  [{lo:2d}-{hi:2d}]: {cnt} ({cnt/len(df)*100:.1f}%)")

print(f"\n=== 总分分布 ===")
bins = list(range(40, 91, 5))
for lo, hi in zip(bins, bins[1:]):
    cnt = ((df["total_score"] >= lo) & (df["total_score"] < hi)).sum()
    if cnt > 0:
        r2 = df.loc[(df["total_score"] >= lo) & (df["total_score"] < hi), "return_2d"].dropna()
        print(f"  [{lo:2d}-{hi:2d}): n={cnt}, 2d_avg={r2.mean():+.2f}%, win={(r2>0).mean()*100:.1f}%")

print(f"\n=== 前向收益随持有期的变化 ===")
for ret_col in ["return_2d", "return_10d", "return_30d", "return_60d"]:
    r = df[ret_col].dropna()
    print(f"  {ret_col}: mean={r.mean():+.2f}%, median={r.median():+.2f}%, win={(r>0).mean()*100:.1f}%, sharpe_approx={r.mean()/r.std()*np.sqrt(250):.2f}")

print(f"\n=== 当前持有期(10天)的实际收益 ===")
r10 = df["return_10d"].dropna()
print(f"  return_10d: n={len(r10)}, avg={r10.mean():+.2f}%, win={ (r10>0).mean()*100:.1f}%")

print(f"\n=== 高ML评分的持有期收益衰减 ===")
for ml_thresh in [10, 11, 12, 13]:
    sub = df[df["score_ml"] >= ml_thresh]
    for ret_col in ["return_2d", "return_10d", "return_30d"]:
        r = sub[ret_col].dropna()
        if len(r) > 10:
            print(f"  ML>={ml_thresh}, {ret_col}: n={len(r)}, mean={r.mean():+.2f}%, win={(r>0).mean()*100:.1f}%")

print(f"\n=== 增强入场条件的损耗 ===")
for rule in ["volume_pass", "ma_alignment_pass", "price_position_pass"]:
    cnt = df[rule].sum()
    print(f"  {rule}: {cnt} ({cnt/len(df)*100:.1f}%)")

rules_passed = df["rules_passed"]
print(f"\n  入场规则通过数分布:")
for n in range(4):
    cnt = (rules_passed == n).sum()
    if cnt > 0:
        print(f"    {n}项: {cnt} ({cnt/len(df)*100:.1f}%)")

print(f"\n=== 策略频次统计 ===")
df["date"] = pd.to_datetime(df["date"])
daily = df.groupby("date").size()
print(f"  有信号的交易日: {len(daily)}/{df['date'].nunique()}")
print(f"  日均信号数: {daily.mean():.1f}")
print(f"  中位数信号: {daily.median():.0f}")
print(f"  最大单日信号: {daily.max()}")
print(f"  信号数分布: <=1:{ (daily<=1).sum() }, 2-3:{ ((daily>=2)&(daily<=3)).sum() }, 4+:{(daily>=4).sum()}")
