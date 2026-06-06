"""生成回测HTML可视化报告"""
import pandas as pd
import numpy as np
import json
from collections import defaultdict

# ── 读取数据 ──
df = pd.read_csv("data/output/backtest_20250529_20260603.csv", dtype={"code": str})
df["date"] = pd.to_datetime(df["date"])

# ── 计算专业风险收益指标 ──

def calc_sharpe(returns, rf=0.02):
    """夏普比率 = (策略年化收益 - 无风险利率) / 年化波动率"""
    r = returns.dropna()
    if len(r) < 5 or r.std() == 0:
        return None
    # 假设每天一个信号，按252个交易日年化
    trading_days = 252
    ann_return = r.mean() * trading_days / 100  # %转小数
    ann_vol = r.std() * np.sqrt(trading_days) / 100
    if ann_vol == 0:
        return None
    return (ann_return - rf) / ann_vol


def calc_max_drawdown(cumulative):
    """计算最大回撤"""
    if len(cumulative) < 2:
        return None
    peak = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - peak) / peak
    return drawdown.min()


def calc_calmar(returns):
    """卡玛比率 = 年化收益率 / 最大回撤绝对值"""
    r = returns.dropna()
    if len(r) < 10:
        return None
    trading_days = 252
    # 构建累计收益曲线
    cum = (1 + r / 100).cumprod()
    mdd = calc_max_drawdown(cum.values)
    if mdd is None or mdd >= 0:
        return None
    ann_return = r.mean() * trading_days / 100
    return round(ann_return / abs(mdd), 2)


def calc_win_loss_ratio(returns):
    """盈亏比 = 平均盈利 / 平均亏损绝对值"""
    r = returns.dropna()
    wins = r[r > 0]
    losses = r[r < 0]
    if len(wins) == 0 or len(losses) == 0:
        return None
    return round(wins.mean() / abs(losses.mean()), 2)


# 对所有持有期计算增强指标
windows = [
    ("2日",  "return_2d",  "bench_2d"),
    ("10日", "return_10d", "bench_10d"),
    ("30日", "return_30d", "bench_30d"),
    ("60日", "return_60d", "bench_60d"),
]

enhanced_metrics = {}
ret_ann = {}
for label, ret_col, ben_col in windows:
    r = df[ret_col].dropna()
    sharpe = calc_sharpe(r)
    calmar = calc_calmar(r)
    wl = calc_win_loss_ratio(r)
    
    # 最大回撤：按天分组取均值后构建累计
    daily_avg = df.groupby("date")[ret_col].mean()
    cum = (1 + daily_avg / 100).cumprod()
    mdd = calc_max_drawdown(cum.values)
    
    enhanced_metrics[label] = {
        "sharpe": sharpe,
        "calmar": calmar,
        "max_drawdown": f"{mdd*100:.2f}%" if mdd is not None and mdd < 0 else "N/A",
        "win_loss_ratio": wl,
    }
    
    trading_days = len(daily_avg)
    ret_ann[label] = r.mean() * 252 / 100 if len(r) > 0 else None

# ── 每月统计 ──
df["month"] = df["date"].dt.strftime("%Y-%m")
monthly = df.groupby("month").agg(
    信号数=("return_2d", "count"),
    均涨2d=("return_2d", "mean"),
    均涨10d=("return_10d", "mean"),
    均涨30d=("return_30d", "mean"),
    均涨60d=("return_60d", "mean"),
    胜率2d=("return_2d", lambda x: (x > 0).mean() * 100),
    胜率10d=("return_10d", lambda x: (x > 0).mean() * 100),
).reset_index()

# ── 汇总统计 ──
total_signals = len(df)
total_stocks = df["code"].nunique()
total_dates = df["date"].nunique()

windows = [
    ("2日",  "return_2d",  "bench_2d"),
    ("10日", "return_10d", "bench_10d"),
    ("30日", "return_30d", "bench_30d"),
    ("60日", "return_60d", "bench_60d"),
]

stats_rows = []
for label, ret_col, ben_col in windows:
    r = df[ret_col].dropna()
    b = df[ben_col].dropna()
    # 对齐索引后比较
    r2 = df[[ret_col, ben_col]].dropna()
    r_vals = r2[ret_col]
    b_vals = r2[ben_col]
    stats_rows.append({
        "持有期": label,
        "信号数": len(r),
        "均涨幅": f"{r.mean():+.2f}%",
        "中位数": f"{r.median():+.2f}%",
        "胜率": f"{(r > 0).mean()*100:.1f}%",
        "最大收益": f"{r.max():+.2f}%",
        "最小收益": f"{r.min():+.2f}%",
        "标准差": f"{r.std():.2f}%",
        "基准均涨": f"{b.mean():+.2f}%",
        "跑赢基准": f"{(r_vals > b_vals).mean()*100:.1f}%",
    })
stats_df = pd.DataFrame(stats_rows)

# ── 每日收益序列 ──
daily_ret = df.groupby("date")["return_2d"].mean().reset_index()
daily_ret.columns = ["date", "策略均涨2d"]
daily_bench = df.groupby("date")["bench_2d"].mean().reset_index()
daily_bench.columns = ["date", "市场基准"]
daily = pd.merge(daily_ret, daily_bench, on="date", how="left").sort_values("date")
daily["date_str"] = daily["date"].dt.strftime("%m-%d")
daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")

# ── 收益分布（直方图数据） ──
hist_data = {}
for label, ret_col, _ in windows:
    r = df[ret_col].dropna()
    bins = 30
    counts, edges = pd.cut(r, bins=bins, retbins=True, right=True)[:2]
    centers = [(edges[i] + edges[i+1])/2 for i in range(len(edges)-1)]
    cats = pd.CategoricalIndex(counts.cat.categories)
    val_counts = counts.value_counts(sort=False)
    val_counts = val_counts.reindex(cats, fill_value=0)
    hist_data[label] = {"centers": [round(c, 2) for c in centers], "counts": val_counts.tolist()}

# ── 热门股票（信号最多的TOP20） ──
top_stocks = df.groupby(["code", "name"]).agg(
    信号次数=("return_2d", "count"),
    均涨2d=("return_2d", "mean"),
    均涨10d=("return_10d", "mean"),
).reset_index().sort_values("信号次数", ascending=False).head(20)

# ── 构建HTML关键部分 ──

# 工具函数
def fmt_val(v):
    try:
        return float(v.replace('%', ''))
    except:
        return 0

def _fmt_metric(v, t='float'):
    if v is None:
        return 'N/A'
    if t == 'float':
        return f'{v:.2f}'
    return str(v)

# 概览卡片
summary_cards = f"""
<div class="card"><div class="num">{total_signals}</div><div class="label">总信号数</div></div>
<div class="card"><div class="num">{total_stocks}</div><div class="label">入选股票</div></div>
<div class="card"><div class="num">{total_dates}</div><div class="label">交易日数</div></div>
<div class="card"><div class="num" style="color:#e74c3c;">{stats_rows[0]['均涨幅']}</div><div class="label">2日均涨幅</div><div class="sub">vs 基准 {stats_rows[0]['基准均涨']}</div></div>
<div class="card"><div class="num" style="color:#e74c3c;">{stats_rows[2]['均涨幅']}</div><div class="label">30日均涨幅</div><div class="sub">vs 基准 {stats_rows[2]['基准均涨']}</div></div>
<div class="card"><div class="num" style="color:#e74c3c;">{stats_rows[3]['均涨幅']}</div><div class="label">60日均涨幅</div><div class="sub">vs 基准 {stats_rows[3]['基准均涨']}</div></div>
<div class="card"><div class="num">{_fmt_metric(enhanced_metrics['2日']['sharpe'], 'float')}</div><div class="label">夏普(2日)</div><div class="sub">卡玛 {_fmt_metric(enhanced_metrics['2日']['calmar'], 'float')}</div></div>
<div class="card"><div class="num" style="color:#8e44ad;">{enhanced_metrics['2日']['max_drawdown']}</div><div class="label">最大回撤(2日)</div><div class="sub">盈亏比 {_fmt_metric(enhanced_metrics['2日']['win_loss_ratio'], 'float')}</div></div>
"""

# 统计表格行
stat_rows_html = ""
for r in stats_rows:
    label = r["持有期"]
    em = enhanced_metrics.get(label, {})
    avg = r["均涨幅"]
    avg_cls = "pos" if fmt_val(avg) > 0 else "neg"
    beat = r["跑赢基准"]
    beat_val = fmt_val(beat)
    beat_cls = "badge-green" if beat_val > 50 else "badge-red"
    stat_rows_html += f"""<tr>
<td><strong>{label}</strong></td>
<td>{r['信号数']}</td>
<td class="{avg_cls}">{avg}</td>
<td>{r['中位数']}</td>
<td>{r['胜率']}</td>
<td>{r['标准差']}</td>
<td>{r['基准均涨']}</td>
<td><span class="badge {beat_cls}">{beat}</span></td>
<td>{_fmt_metric(em.get('sharpe'), 'float')}</td>
<td>{_fmt_metric(em.get('calmar'), 'float')}</td>
<td>{em.get('max_drawdown', 'N/A')}</td>
<td>{_fmt_metric(em.get('win_loss_ratio'), 'float')}</td>
</tr>"""

# TOP股票行
top_rows_html = ""
for i, r in top_stocks.iterrows():
    d2 = r["均涨2d"]
    d10 = r["均涨10d"]
    d2_cls = "pos" if d2 > 0 else "neg"
    d10_cls = "pos" if d10 > 0 else "neg"
    top_rows_html += f"""<tr><td>{top_stocks.index.get_loc(i)+1}</td><td>{r['code']}</td><td>{r['name']}</td><td><strong>{int(r['信号次数'])}</strong></td><td class="{d2_cls}">{d2:+.2f}%</td><td class="{d10_cls}">{d10:+.2f}%</td></tr>"""

# ── 组装完整HTML ──
html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>回测报告 2025-05-29 ~ 2026-06-03</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, 'Microsoft YaHei', sans-serif; background: #f0f2f5; color: #333; }}
.header {{ background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); color: white; padding: 40px 30px; text-align: center; }}
.header h1 {{ font-size: 28px; margin-bottom: 8px; }}
.header p {{ opacity: 0.8; font-size: 14px; }}
.summary-cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; padding: 24px 30px; max-width: 1400px; margin: 0 auto; }}
.card {{ background: white; border-radius: 12px; padding: 20px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
.card .num {{ font-size: 32px; font-weight: 700; color: #1a1a2e; }}
.card .label {{ font-size: 13px; color: #888; margin-top: 4px; }}
.card .sub {{ font-size: 11px; color: #aaa; margin-top: 2px; }}
.section {{ max-width: 1400px; margin: 20px auto; padding: 0 30px; }}
.section-title {{ font-size: 18px; font-weight: 600; margin-bottom: 16px; padding-left: 12px; border-left: 4px solid #0f3460; }}
.chart-container {{ background: white; border-radius: 12px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 24px; }}
.chart-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{ background: #f8f9fa; padding: 10px 12px; text-align: center; font-weight: 600; border-bottom: 2px solid #dee2e6; }}
td {{ padding: 8px 12px; text-align: center; border-bottom: 1px solid #eee; }}
tr:hover td {{ background: #f8f9ff; }}
.pos {{ color: #e74c3c; }}
.neg {{ color: #27ae60; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
.badge-green {{ background: #d4edda; color: #155724; }}
.badge-red {{ background: #f8d7da; color: #721c24; }}
@media (max-width: 768px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>

<div class="header">
<h1>📊 选股逻辑回测报告</h1>
<p>回测区间: 2025-05-29 ~ 2026-06-03 · {total_signals} 条信号 · {total_stocks} 只股票 · {total_dates} 个交易日</p>
<p style="margin-top:6px;font-size:13px;opacity:0.6;">选股条件: MACD金叉 ∩ ZJTJ控盘信号 + KDJ辅助验证</p>
</div>

<div class="summary-cards">
{summary_cards}
</div>

<!-- 各持有期对比 -->
<div class="section">
<div class="section-title">各持有期收益对比</div>
<div class="chart-container">
<table>
<thead><tr>
<th>持有期</th><th>信号数</th><th>均涨幅</th><th>中位数</th><th>胜率</th><th>标准差</th><th>基准均涨</th><th>跑赢基准</th><th>夏普</th><th>卡玛</th><th>最大回撤</th><th>盈亏比</th>
</tr></thead>
<tbody>
{stat_rows_html}
</tbody>
</table>
</div>
</div>

<!-- 图表行 -->
<div class="section">
<div class="chart-row">
<div class="chart-container"><canvas id="chart_ret"></canvas></div>
<div class="chart-container"><canvas id="chart_winrate"></canvas></div>
</div>
</div>

<div class="section">
<div class="section-title">每日策略收益 vs 市场基准（2日持有期）</div>
<div class="chart-container"><canvas id="chart_daily"></canvas></div>
</div>

<div class="section">
<div class="section-title">收益分布</div>
<div class="chart-row">
<div class="chart-container"><canvas id="hist_2d"></canvas></div>
<div class="chart-container"><canvas id="hist_10d"></canvas></div>
</div>
</div>

<div class="section">
<div class="section-title">月度表现</div>
<div class="chart-container"><canvas id="chart_monthly"></canvas></div>
</div>

<div class="section">
<div class="section-title">信号频率 TOP 20 股票</div>
<div class="chart-container">
<div style="overflow-x:auto;">
<table>
<thead><tr><th>#</th><th>代码</th><th>名称</th><th>信号次数</th><th>2日均涨</th><th>10日均涨</th></tr></thead>
<tbody>
{top_rows_html}
</tbody>
</table>
</div>
</div>
</div>

<script>
Chart.defaults.font.family = "'Microsoft YaHei', sans-serif";
const labels = ['2日', '10日', '30日', '60日'];

const stats = {json.dumps(stats_rows)};
const avgRets = stats.map(r => parseFloat(r['均涨幅']));
const benchRets = stats.map(r => parseFloat(r['基准均涨']));

new Chart(document.getElementById('chart_ret'), {{
type: 'bar',
data: {{
labels,
datasets: [
{{ label: '策略均涨幅', data: avgRets, backgroundColor: 'rgba(231,76,60,0.8)', borderRadius: 4 }},
{{ label: '市场基准', data: benchRets, backgroundColor: 'rgba(52,152,219,0.8)', borderRadius: 4 }}
]
}},
options: {{
responsive: true,
plugins: {{ legend: {{ position: 'top' }}, title: {{ display: true, text: '各持有期均涨幅对比', font: {{ size: 15 }} }} }},
scales: {{ y: {{ ticks: {{ callback: v => v+'%' }} }} }}
}}
}});

const winRates = stats.map(r => parseFloat(r['胜率']));
const beatRates = stats.map(r => parseFloat(r['跑赢基准']));
new Chart(document.getElementById('chart_winrate'), {{
type: 'bar',
data: {{
labels,
datasets: [
{{ label: '胜率', data: winRates, backgroundColor: 'rgba(46,204,113,0.8)', borderRadius: 4 }},
{{ label: '跑赢基准', data: beatRates, backgroundColor: 'rgba(52,152,219,0.8)', borderRadius: 4 }}
]
}},
options: {{
responsive: true,
plugins: {{ legend: {{ position: 'top' }}, title: {{ display: true, text: '胜率 vs 跑赢基准', font: {{ size: 15 }} }} }},
scales: {{ y: {{ min: 0, max: 100, ticks: {{ callback: v => v+'%' }} }} }}
}}
}});

const dailyData = {json.dumps(daily.to_dict(orient='records'))};
new Chart(document.getElementById('chart_daily'), {{
type: 'line',
data: {{
labels: dailyData.map(d => d.date_str),
datasets: [
{{ label: '策略均涨2d', data: dailyData.map(d => d['策略均涨2d']), borderColor: '#e74c3c', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0 }},
{{ label: '市场基准', data: dailyData.map(d => d['市场基准']), borderColor: '#3498db', backgroundColor: 'transparent', tension: 0.3, pointRadius: 0, borderDash: [5,5] }}
]
}},
options: {{
responsive: true, interaction: {{ mode: 'index', intersect: false }},
plugins: {{ legend: {{ position: 'top' }}, title: {{ display: true, text: '每日收益走势（2日持有期）', font: {{ size: 15 }} }} }},
scales: {{ y: {{ ticks: {{ callback: v => v+'%' }} }} }}
}}
}});

const monthly = {json.dumps(monthly.to_dict(orient='records'))};
new Chart(document.getElementById('chart_monthly'), {{
type: 'bar',
data: {{
labels: monthly.map(m => m.month),
datasets: [
{{ label: '信号数', data: monthly.map(m => m['信号数']), backgroundColor: 'rgba(52,152,219,0.7)', yAxisID: 'y', order: 2, borderRadius: 3 }},
{{ label: '2日均涨%', data: monthly.map(m => m['均涨2d']), type: 'line', borderColor: '#e74c3c', backgroundColor: 'transparent', tension: 0.3, pointBackgroundColor: '#e74c3c', yAxisID: 'y1', order: 1 }},
{{ label: '胜率%', data: monthly.map(m => m['胜率2d']), type: 'line', borderColor: '#2ecc71', backgroundColor: 'transparent', tension: 0.3, pointBackgroundColor: '#2ecc71', yAxisID: 'y1', order: 1, borderDash: [4,4] }}
]
}},
options: {{
responsive: true,
plugins: {{ legend: {{ position: 'top' }}, title: {{ display: true, text: '月度表现', font: {{ size: 15 }} }} }},
scales: {{
y: {{ position: 'left', beginAtZero: true, title: {{ display: true, text: '信号数' }} }},
y1: {{ position: 'right', grid: {{ drawOnChartArea: false }}, ticks: {{ callback: v => v+'%' }} }}
}}
}}
}});

function makeHist(id, label) {{
const hd = {json.dumps(hist_data)};
const d = hd[label];
new Chart(document.getElementById(id), {{
type: 'bar',
data: {{
labels: d.centers.map(v => v.toFixed(1)+'%'),
datasets: [{{ label: '频数', data: d.counts, backgroundColor: 'rgba(52,152,219,0.6)', borderRadius: 3 }}]
}},
options: {{
responsive: true,
plugins: {{ legend: {{ display: false }}, title: {{ display: true, text: label+'收益分布', font: {{ size: 13 }} }} }},
scales: {{ x: {{ ticks: {{ maxTicksLimit: 15, callback: v => v }} }}, y: {{ beginAtZero: true }} }}
}}
}});
}}
makeHist('hist_2d', '2日');
makeHist('hist_10d', '10日');
</script>
</body>
</html>"""

# ── 写入文件 ──
output_path = "data/output/backtest_report_20250529_20260603.html"
with open(output_path, "w", encoding="utf-8") as f:
    f.write(html)

print(f"✅ 报告已生成: {output_path}")
