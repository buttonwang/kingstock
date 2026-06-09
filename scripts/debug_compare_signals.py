"""对比plot脚本信号 vs CSV信号的内容差异"""
import os, sys, pandas as pd, numpy as np
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

# CSV信号 (backtest生成)
df_csv = pd.read_csv("data/output/backtest_ml_20230601_20260603.csv")
df_csv["date"] = pd.to_datetime(df_csv["date"]).dt.strftime("%Y-%m-%d")
df_csv["code"] = df_csv["code"].astype(str).str.zfill(6)
print(f"CSV信号: {len(df_csv)}")
print(f"  total_score: mean={df_csv['total_score'].mean():.1f}, min={df_csv['total_score'].min()}, max={df_csv['total_score'].max()}")
print(f"  score_ml: mean={df_csv['score_ml'].mean():.1f}, min={df_csv['score_ml'].min():.1f}, max={df_csv['score_ml'].max():.1f}")
print(f"  max_score: {df_csv['max_score'].unique()}")
print(f"  market_state: {df_csv['market_state'].value_counts().to_dict()}")
print(f"  日期范围: {df_csv['date'].min()} ~ {df_csv['date'].max()}")
print(f"  唯一日期: {df_csv['date'].nunique()}, 唯一股票: {df_csv['code'].nunique()}")

# 每日信号数分布
daily_csv = df_csv.groupby('date').size()
print(f"  每日信号: mean={daily_csv.mean():.1f}, max={daily_csv.max()}")

# 检查: 哪些日期的信号差异最大
print(f"\n=== 信号质量分析 ===")
# score_ml分布
for v in sorted(df_csv['score_ml'].unique()):
    n = (df_csv['score_ml'] == v).sum()
    print(f"  score_ml={v}: {n} ({n/len(df_csv)*100:.1f}%)")

# total_score分布
print(f"\n  total_score分段:")
for lo, hi in [(50,60),(60,70),(70,80),(80,90),(90,100)]:
    n = ((df_csv['total_score']>=lo) & (df_csv['total_score']<hi)).sum()
    print(f"    [{lo},{hi}): {n}")
