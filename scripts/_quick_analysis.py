"""快速分析V1.1和V1.4信号数据"""
import pandas as pd

# V1.1
df11 = pd.read_csv("data/output/v1_1_signals_debug.csv")
print(f"=== V1.1 信号 ===")
print(f"信号数: {len(df11)}")
print(f"市场: {df11['market_state'].value_counts().to_dict()}")
print(f"ML: mean={df11['score_ml'].mean():.1f}, min={df11['score_ml'].min():.1f}")
print(f"日期范围: {df11['date'].min()} ~ {df11['date'].max()}")
print(f"唯一交易日: {df11['date'].nunique()}, 唯一股票: {df11['code'].nunique()}")
per_day = df11.groupby('date').size()
print(f"每日信号: mean={per_day.mean():.1f}, median={per_day.median()}")

# V1.4
df14 = pd.read_csv("data/output/v1_4_signals_debug.csv")
print(f"\n=== V1.4 信号 ===")
print(f"信号数: {len(df14)}")
if 'signal_tier' in df14.columns:
    print(f"Tier分布: {df14['signal_tier'].value_counts().sort_index().to_dict()}")
print(f"市场: {df14['market_state'].value_counts().to_dict()}")
