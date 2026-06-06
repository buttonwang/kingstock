"""股市风格HTML报告生成器 - 生成美观的选股分析报告网页"""

import os
from datetime import datetime

import pandas as pd


def _fmt_pct(val, default="--"):
    """格式化百分数，正数加+号"""
    if val is None:
        return default
    try:
        v = float(val)
        if v > 0:
            return f"+{v:.2f}%"
        elif v < 0:
            return f"{v:.2f}%"
        return "0.00%"
    except (ValueError, TypeError):
        return default


def _pct_class(val):
    """返回涨跌CSS class"""
    if val is None:
        return "flat"
    try:
        v = float(val)
        if v > 0:
            return "up"
        elif v < 0:
            return "down"
        return "flat"
    except (ValueError, TypeError):
        return "flat"


def _safe(val, default=""):
    """安全取值"""
    if val is None:
        return default
    if isinstance(val, float) and pd.isna(val):
        return default
    return str(val)


def _stock_url(code: str) -> str:
    """根据股票代码返回雪球链接（自动判断深市/沪市前缀）"""
    code = code.strip()
    if code.startswith("6") or code.startswith("9"):
        prefix = "SH"
    else:
        prefix = "SZ"
    return f"https://xueqiu.com/S/{prefix}{code}"


def _build_styles() -> str:
    """生成CSS样式"""
    return """\
<style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                     "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
        background: #0d1117;
        color: #c9d1d9;
        padding: 20px;
        line-height: 1.6;
    }
    .container { max-width: 1400px; margin: 0 auto; }

    /* Header */
    .report-header {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        border-radius: 12px;
        padding: 30px 40px;
        margin-bottom: 24px;
        border: 1px solid #30363d;
        text-align: center;
    }
    .report-header h1 {
        font-size: 28px;
        color: #e6edf3;
        letter-spacing: 2px;
        margin-bottom: 8px;
    }
    .report-header .subtitle {
        font-size: 14px;
        color: #8b949e;
    }
    .report-header .date-badge {
        display: inline-block;
        background: #58a6ff22;
        color: #58a6ff;
        padding: 4px 16px;
        border-radius: 20px;
        font-size: 16px;
        font-weight: 600;
        border: 1px solid #58a6ff44;
        margin-top: 8px;
    }

    /* Stats Cards */
    .stats-row {
        display: flex;
        gap: 16px;
        margin-bottom: 24px;
        flex-wrap: wrap;
    }
    .stat-card {
        flex: 1;
        min-width: 160px;
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 10px;
        padding: 20px;
        text-align: center;
        transition: transform 0.2s, border-color 0.2s;
    }
    .stat-card:hover {
        transform: translateY(-2px);
        border-color: #58a6ff;
    }
    .stat-card .num {
        font-size: 32px;
        font-weight: 700;
    }
    .stat-card .label {
        font-size: 13px;
        color: #8b949e;
        margin-top: 4px;
    }
    .stat-card.king .num { color: #ffd700; }
    .stat-card.main .num { color: #58a6ff; }
    .stat-card.manual .num { color: #bc8cff; }
    .stat-card.ebk .num { color: #79c0ff; }

    /* Section Card */
    .section-card {
        background: #161b22;
        border: 1px solid #30363d;
        border-radius: 10px;
        margin-bottom: 20px;
        overflow: hidden;
    }
    .section-header {
        background: #1c2333;
        padding: 16px 24px;
        border-bottom: 1px solid #30363d;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
    }
    .section-header h2 {
        font-size: 18px;
        font-weight: 600;
        color: #e6edf3;
    }
    .section-header .badge {
        font-size: 12px;
        padding: 3px 12px;
        border-radius: 12px;
        background: #30363d;
        color: #8b949e;
    }
    .section-header .badge.green { background: #27ae6033; color: #27ae60; }
    .section-header .badge.blue { background: #58a6ff33; color: #58a6ff; }
    .section-header .badge.gold { background: #ffd70033; color: #ffd700; }

    /* Tables */
    .table-wrap {
        overflow-x: auto;
        padding: 0;
    }
    table {
        width: 100%;
        border-collapse: collapse;
        font-size: 13px;
    }
    thead th {
        background: #1c2333;
        color: #8b949e;
        font-weight: 600;
        text-align: center;
        padding: 12px 10px;
        border-bottom: 2px solid #30363d;
        white-space: nowrap;
        position: sticky;
        top: 0;
        z-index: 1;
    }
    tbody tr {
        border-bottom: 1px solid #21262d;
        transition: background 0.15s;
    }
    tbody tr:hover { background: #1c2333; }
    tbody tr:nth-child(even) { background: #0d1117; }
    tbody tr:nth-child(even):hover { background: #1c2333; }
    tbody td {
        padding: 10px 10px;
        text-align: center;
        white-space: nowrap;
    }

    /* 涨跌颜色 */
    .up { color: #e74c3c; font-weight: 600; }
    .down { color: #27ae60; font-weight: 600; }
    .flat { color: #8b949e; }

    /* 股票代码 */
    .code-link {
        color: #58a6ff;
        text-decoration: none;
        font-family: "Consolas", "Courier New", monospace;
        font-weight: 600;
    }
    .code-link:hover { text-decoration: underline; }

    /* 股票名称链接 */
    .name-link {
        color: #e6edf3;
        text-decoration: none;
        font-weight: 500;
    }
    .name-link:hover {
        color: #58a6ff;
        text-decoration: underline;
    }

    /* 标记符号 */
    .check-mark { color: #27ae60; font-size: 14px; }
    .cross-mark { color: #8b949e; font-size: 14px; }

    /* King Stock 行 */
    .king-row { background: #332b00 !important; }
    .king-row td { color: #ffd700; }
    .king-row:nth-child(even) { background: #3d3400 !important; }

    /* 重点关注行（连选 >= 3天） */
    .focus-row { background: #1a3a1a !important; }
    .focus-row td { color: #7ecb7e; }
    .focus-row:nth-child(even) { background: #1f4520 !important; }

    /* King 区域 */
    .king-section {
        background: linear-gradient(135deg, #1a1500 0%, #2a2200 100%);
        border: 2px solid #ffd70055;
        border-radius: 10px;
        padding: 24px;
        margin-bottom: 20px;
        text-align: center;
    }
    .king-section h2 {
        color: #ffd700;
        font-size: 22px;
        margin-bottom: 8px;
    }
    .king-section .king-stocks {
        display: flex;
        justify-content: center;
        gap: 20px;
        flex-wrap: wrap;
        margin-top: 12px;
    }
    .king-stock-item {
        background: #332b00;
        border: 1px solid #ffd70044;
        border-radius: 8px;
        padding: 12px 24px;
        font-size: 16px;
        font-weight: 600;
        color: #ffd700;
    }
    .king-stock-item .king-code { color: #58a6ff; }

    /* Rule Summary */
    .rule-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
        gap: 12px;
        padding: 20px 24px;
    }
    .rule-item {
        background: #0d1117;
        border: 1px solid #21262d;
        border-radius: 8px;
        padding: 12px 16px;
        text-align: center;
    }
    .rule-item .rlabel { font-size: 12px; color: #8b949e; }
    .rule-item .rnum { font-size: 24px; font-weight: 700; margin: 4px 0; }
    .rule-item .rtype {
        font-size: 11px;
        padding: 2px 8px;
        border-radius: 8px;
        display: inline-block;
    }
    .rtype.core { background: #27ae6033; color: #27ae60; }
    .rtype.bonus { background: #58a6ff33; color: #58a6ff; }
    .rtype.init { background: #8b949e33; color: #8b949e; }

    /* 空状态 */
    .empty-state {
        padding: 40px;
        text-align: center;
        color: #8b949e;
        font-size: 14px;
    }

    /* Footer */
    .report-footer {
        text-align: center;
        padding: 20px;
        color: #484f58;
        font-size: 12px;
        border-top: 1px solid #21262d;
        margin-top: 20px;
    }

    /* Responsive */
    @media (max-width: 768px) {
        body { padding: 10px; }
        .report-header { padding: 20px; }
        .report-header h1 { font-size: 20px; }
        .stat-card { min-width: 120px; }
        .stat-card .num { font-size: 24px; }
    }
</style>"""


def _build_header(date_str: str, result_df, manual_df, ebk_df, king_set) -> str:
    """生成报告头部 + 统计卡片"""
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # 统计
    main_count = len(result_df) if result_df is not None and not result_df.empty else 0
    manual_count = 0
    if manual_df is not None and not manual_df.empty and "core_pass" in manual_df.columns:
        manual_count = int(manual_df["core_pass"].sum())
    elif manual_df is not None and not manual_df.empty:
        manual_count = len(manual_df)

    ebk_count = 0
    if ebk_df is not None and not ebk_df.empty and "core_pass" in ebk_df.columns:
        ebk_count = int(ebk_df["core_pass"].sum())
    elif ebk_df is not None and not ebk_df.empty:
        ebk_count = len(ebk_df)

    king_count = len(king_set) if king_set else 0

    return f"""\
<div class="report-header">
    <h1>A股智能选股分析报告</h1>
    <div class="date-badge">{display_date}</div>
    <div class="subtitle" style="margin-top:12px;">
        核心条件：MACD买入信号 ∩ ZJTJ庄家控盘 &nbsp;|&nbsp; 加分项：KDJ买入 + 财务增长
    </div>
</div>

<div class="stats-row">
    <div class="stat-card main">
        <div class="num">{main_count}</div>
        <div class="label">主选股结果</div>
    </div>
    <div class="stat-card manual">
        <div class="num">{manual_count}</div>
        <div class="label">手工选股满足核心</div>
    </div>
    <div class="stat-card ebk">
        <div class="num">{ebk_count}</div>
        <div class="label">龙头EBK满足核心</div>
    </div>
    <div class="stat-card king">
        <div class="num">{king_count}</div>
        <div class="label">👑 King Stock</div>
    </div>
</div>"""


def _build_rule_summary(stage_details: dict, stage_codes: dict, total_stocks: int) -> str:
    """生成规则筛选汇总"""
    if not stage_details:
        return ""

    rule_config = {
        "1_RPS板块": ("板块RPS筛选", "初始池"),
        "2_MACD": ("MACD买入信号", "核心"),
        "3_ZJTJ": ("ZJTJ庄家控盘", "核心"),
        "4_KDJ": ("KDJ买入信号", "加分项"),
        "5_财务": ("财务基本面", "加分项"),
    }

    items = []
    for key, (label, rtype) in rule_config.items():
        codes = stage_codes.get(key, set()) if stage_codes else set()
        if key == "1_RPS板块":
            inp = total_stocks
        else:
            pool = stage_codes.get("1_RPS板块", set()) if stage_codes else set()
            inp = len(pool)
        out = len(codes)

        if rtype == "核心":
            rtype_cls = "core"
        elif rtype == "初始池":
            rtype_cls = "init"
        else:
            rtype_cls = "bonus"

        items.append(f"""\
        <div class="rule-item">
            <div class="rlabel">{label}</div>
            <div class="rnum" style="color:#e6edf3">{out}</div>
            <div style="font-size:11px;color:#8b949e;">{inp}只 &rarr; {out}只</div>
            <span class="rtype {rtype_cls}">{rtype}</span>
        </div>""")

    return f"""\
<div class="section-card">
    <div class="section-header">
        <h2>规则筛选汇总</h2>
        <span class="badge">各规则筛选结果</span>
    </div>
    <div class="rule-grid">
        {''.join(items)}
    </div>
</div>"""


def _build_table(data_rows: list, columns: list, king_set: set = None, focus_set: set = None) -> str:
    """通用表格生成

    参数:
        data_rows: [{col_name: value, ...}, ...]
        columns: [(col_name, display_name, align), ...]
        king_set: King Stock 代码集合
        focus_set: 重点关注代码集合（连选 >= 3天）
    """
    if not data_rows:
        return """<div class="empty-state">暂无数据</div>"""

    thead = "<thead><tr>"
    for _, display_name, _ in columns:
        thead += f"<th>{display_name}</th>"
    thead += "</tr></thead>"

    tbody = "<tbody>"
    for row in data_rows:
        code = str(row.get("code", ""))
        is_king = king_set and code in king_set
        is_focus = focus_set and code in focus_set
        if is_king:
            tr_class = ' class="king-row"'
        elif is_focus:
            tr_class = ' class="focus-row"'
        else:
            tr_class = ""
        tbody += f"<tr{tr_class}>"
        for col_key, _, _ in columns:
            val = row.get(col_key, "")
            tbody += f"<td>{val}</td>"
        tbody += "</tr>"
    tbody += "</tbody>"

    return f"""\
<div class="table-wrap">
    <table>{thead}{tbody}</table>
</div>"""


def _build_main_table(result_df: pd.DataFrame, tracker_data: dict, king_set: set) -> str:
    """生成主选股结果表"""
    if result_df is None or result_df.empty:
        return """<div class="empty-state">今日无股票满足核心条件(MACD ∩ ZJTJ)</div>"""

    columns = [
        ("code", "代码", "center"),
        ("name", "名称", "center"),
        ("sector", "板块", "center"),
        ("dif", "DIF", "right"),
        ("dea", "DEA", "right"),
        ("macd", "MACD", "right"),
        ("k", "K", "right"),
        ("d", "D", "right"),
        ("j", "J", "right"),
        ("kongpan", "控盘度", "right"),
        ("kdj_bonus", "KDJ加分", "center"),
        ("finance_bonus", "财务加分", "center"),
        ("bonus_total", "加分合计", "center"),
        ("change_pct", "今日涨跌", "right"),
        ("consecutive", "连选天数", "center"),
        ("focus", "重点关注", "center"),
        ("return_3d", "近3日", "right"),
        ("return_5d", "近5日", "right"),
        ("return_10d", "近10日", "right"),
    ]

    # 计算重点关注集合（连选 >= 3天）
    focus_set = {code for code, td in (tracker_data or {}).items()
                 if td.get("consecutive_days", 0) >= 3}

    rows = []
    for _, row in result_df.iterrows():
        code = str(row.get("code", ""))
        td = (tracker_data or {}).get(code, {})

        kdj = _safe(row.get("KDJ加分", ""))
        fin = _safe(row.get("财务加分", ""))
        bonus = int(row.get("加分合计", 0))

        chg = td.get("change_pct")
        chg_str = f'<span class="{_pct_class(chg)}">{_fmt_pct(chg)}</span>' if chg is not None else "--"

        r3 = td.get("return_3d")
        r3_str = f'<span class="{_pct_class(r3)}">{_fmt_pct(r3)}</span>' if r3 is not None else "--"

        r5 = td.get("return_5d")
        r5_str = f'<span class="{_pct_class(r5)}">{_fmt_pct(r5)}</span>' if r5 is not None else "--"

        r10 = td.get("return_10d")
        r10_str = f'<span class="{_pct_class(r10)}">{_fmt_pct(r10)}</span>' if r10 is not None else "--"

        cons = td.get("consecutive_days", 1)
        cons_str = f'<span style="font-weight:600;color:#58a6ff">{cons}</span>' if cons > 1 else str(cons)
        focus_str = '<span style="font-size:16px;">🌟</span>' if cons >= 3 else ""

        rows.append({
            "code": f'<a class="code-link" href="{_stock_url(code)}" target="_blank">{code}</a>',
            "name": f'<a class="name-link" href="{_stock_url(code)}" target="_blank">{row.get("name", "")}</a>',
            "sector": row.get("sector", ""),
            "dif": f"{row.get('dif', 0):+.4f}",
            "dea": f"{row.get('dea', 0):+.4f}",
            "macd": f"{row.get('macd', 0):+.4f}",
            "k": f"{row.get('k', 0):.2f}",
            "d": f"{row.get('d', 0):.2f}",
            "j": f"{row.get('j', 0):.2f}",
            "kongpan": f"{row.get('kongpan', 0):.2f}",
            "kdj_bonus": f'<span class="check-mark">&#10004;</span>' if kdj else "",
            "finance_bonus": f'<span class="check-mark">&#10004;</span>' if fin else "",
            "bonus_total": f'<span style="font-weight:600">{bonus}</span>',
            "change_pct": chg_str,
            "consecutive": cons_str,
            "focus": focus_str,
            "return_3d": r3_str,
            "return_5d": r5_str,
            "return_10d": r10_str,
        })

    return _build_table(rows, columns, king_set, focus_set)


def _build_manual_table(manual_df: pd.DataFrame, tracker_data: dict, king_set: set) -> str:
    """生成手工选股结果表"""
    if manual_df is None or manual_df.empty:
        return """<div class="empty-state">今日无手工选股数据</div>"""

    # 计算重点关注集合（连选 >= 3天）
    focus_set = {code for code, td in (tracker_data or {}).items()
                 if td.get("consecutive_days", 0) >= 3}

    columns = [
        ("code", "代码", "center"),
        ("name", "名称", "center"),
        ("macd", "MACD", "right"),
        ("k", "K", "right"),
        ("d", "D", "right"),
        ("j", "J", "right"),
        ("kongpan", "控盘度", "right"),
        ("rule_detail", "规则详情", "left"),
        ("core_pass", "核心条件", "center"),
        ("change_pct", "今日涨跌", "right"),
        ("consecutive", "连选天数", "center"),
        ("focus", "重点关注", "center"),
        ("return_3d", "近3日", "right"),
        ("return_5d", "近5日", "right"),
        ("return_10d", "近10日", "right"),
    ]

    rows = []
    for _, row in manual_df.iterrows():
        code = str(row.get("code", ""))
        td = (tracker_data or {}).get(code, {})

        is_core = row.get("core_pass", False)
        core_str = '<span class="check-mark">&#10004;</span>' if is_core else '<span class="cross-mark">&#10008;</span>'

        chg = td.get("change_pct")
        chg_str = f'<span class="{_pct_class(chg)}">{_fmt_pct(chg)}</span>' if chg is not None else "--"

        r3 = td.get("return_3d")
        r3_str = f'<span class="{_pct_class(r3)}">{_fmt_pct(r3)}</span>' if r3 is not None else "--"
        r5 = td.get("return_5d")
        r5_str = f'<span class="{_pct_class(r5)}">{_fmt_pct(r5)}</span>' if r5 is not None else "--"
        r10 = td.get("return_10d")
        r10_str = f'<span class="{_pct_class(r10)}">{_fmt_pct(r10)}</span>' if r10 is not None else "--"

        cons = td.get("consecutive_days", 1)
        cons_str = f'<span style="font-weight:600;color:#58a6ff">{cons}</span>' if cons > 1 else str(cons)
        focus_str = '<span style="font-size:16px;">🌟</span>' if cons >= 3 else ""

        # 规则详情截断显示
        detail = str(row.get("rule_detail", ""))
        if len(detail) > 60:
            detail = detail[:57] + "..."

        rows.append({
            "code": f'<a class="code-link" href="{_stock_url(code)}" target="_blank">{code}</a>',
            "name": f'<a class="name-link" href="{_stock_url(code)}" target="_blank">{row.get("name", "")}</a>',
            "macd": f"{row.get('macd', 0):+.4f}",
            "k": f"{row.get('k', 0):.2f}",
            "d": f"{row.get('d', 0):.2f}",
            "j": f"{row.get('j', 0):.2f}",
            "kongpan": f"{row.get('kongpan', 0):.2f}",
            "rule_detail": detail,
            "core_pass": core_str,
            "change_pct": chg_str,
            "consecutive": cons_str,
            "focus": focus_str,
            "return_3d": r3_str,
            "return_5d": r5_str,
            "return_10d": r10_str,
        })

    return _build_table(rows, columns, king_set, focus_set)


def _build_ebk_table(ebk_df: pd.DataFrame, tracker_data: dict, king_set: set) -> str:
    """生成龙头EBK结果表（仅显示满足核心条件的股票）"""
    if ebk_df is None or ebk_df.empty:
        return """<div class="empty-state">今日无龙头EBK数据</div>"""

    core_df = ebk_df[ebk_df["core_pass"]].copy() if "core_pass" in ebk_df.columns else ebk_df

    if core_df.empty:
        return """<div class="empty-state">龙头EBK分析中无股票满足核心条件(MACD ∩ ZJTJ)</div>"""

    # 计算重点关注集合（连选 >= 3天）
    focus_set = {code for code, td in (tracker_data or {}).items()
                 if td.get("consecutive_days", 0) >= 3}

    columns = [
        ("code", "代码", "center"),
        ("name", "名称", "center"),
        ("macd", "MACD", "right"),
        ("k", "K", "right"),
        ("d", "D", "right"),
        ("j", "J", "right"),
        ("kongpan", "控盘度", "right"),
        ("rule_detail", "规则详情", "left"),
        ("change_pct", "今日涨跌", "right"),
        ("consecutive", "连选天数", "center"),
        ("focus", "重点关注", "center"),
        ("return_3d", "近3日", "right"),
        ("return_5d", "近5日", "right"),
        ("return_10d", "近10日", "right"),
    ]

    rows = []
    for _, row in core_df.iterrows():
        code = str(row.get("code", ""))
        td = (tracker_data or {}).get(code, {})

        chg = td.get("change_pct")
        chg_str = f'<span class="{_pct_class(chg)}">{_fmt_pct(chg)}</span>' if chg is not None else "--"

        r3 = td.get("return_3d")
        r3_str = f'<span class="{_pct_class(r3)}">{_fmt_pct(r3)}</span>' if r3 is not None else "--"
        r5 = td.get("return_5d")
        r5_str = f'<span class="{_pct_class(r5)}">{_fmt_pct(r5)}</span>' if r5 is not None else "--"
        r10 = td.get("return_10d")
        r10_str = f'<span class="{_pct_class(r10)}">{_fmt_pct(r10)}</span>' if r10 is not None else "--"

        cons = td.get("consecutive_days", 1)
        cons_str = f'<span style="font-weight:600;color:#58a6ff">{cons}</span>' if cons > 1 else str(cons)
        focus_str = '<span style="font-size:16px;">🌟</span>' if cons >= 3 else ""

        detail = str(row.get("rule_detail", ""))
        if len(detail) > 60:
            detail = detail[:57] + "..."

        rows.append({
            "code": f'<a class="code-link" href="{_stock_url(code)}" target="_blank">{code}</a>',
            "name": f'<a class="name-link" href="{_stock_url(code)}" target="_blank">{row.get("name", "")}</a>',
            "macd": f"{row.get('macd', 0):+.4f}",
            "k": f"{row.get('k', 0):.2f}",
            "d": f"{row.get('d', 0):.2f}",
            "j": f"{row.get('j', 0):.2f}",
            "kongpan": f"{row.get('kongpan', 0):.2f}",
            "rule_detail": detail,
            "change_pct": chg_str,
            "consecutive": cons_str,
            "focus": focus_str,
            "return_3d": r3_str,
            "return_5d": r5_str,
            "return_10d": r10_str,
        })

    return _build_table(rows, columns, king_set, focus_set)


def _build_king_section(king_set: set, stock_name_map: dict) -> str:
    """生成King Stock高亮区域"""
    if not king_set:
        return ""

    items = []
    for code in sorted(king_set):
        name = stock_name_map.get(code, "")
        items.append(f"""\
            <div class="king-stock-item">
                <span class="king-code">{code}</span>
                <span style="color:#e6edf3;margin-left:8px;"><a class="name-link" href="{_stock_url(code)}" target="_blank">{name}</a></span>
            </div>""")

    return f"""\
<div class="king-section">
    <h2>&#128081; King Stock &#128081;</h2>
    <div style="color:#8b949e;font-size:13px;margin-bottom:8px;">
        以下股票在「主选股 + 手工选股 + 龙头EBK」三部分中均满足核心条件
    </div>
    <div class="king-stocks">
        {''.join(items)}
    </div>
</div>"""


# ------------------------------------------------------------------
# 公开 API
# ------------------------------------------------------------------


def build_html_report(
    date_str: str,
    result_df: pd.DataFrame = None,
    stage_details: dict = None,
    stage_codes: dict = None,
    total_stocks: int = 0,
    manual_df: pd.DataFrame = None,
    ebk_df: pd.DataFrame = None,
    king_set: set = None,
    stock_name_map: dict = None,
    tracker_data: dict = None,
) -> str:
    """生成完整的股市风格HTML报告

    参数:
        date_str: 日期 YYYYMMDD
        result_df: 主选股结果
        stage_details: 各阶段明细
        stage_codes: 各阶段代码集合
        total_stocks: 初始总数
        manual_df: 手工选股结果
        ebk_df: 龙头EBK结果
        king_set: King Stock代码集合
        stock_name_map: {code: name} 映射
        tracker_data: {code: {change_pct, return_3d/5d/10d, consecutive_days}}

    返回:
        HTML 字符串
    """
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = f"""\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>A股智能选股分析报告 - {display_date}</title>
    {_build_styles()}
</head>
<body>
<div class="container">

    {_build_header(date_str, result_df, manual_df, ebk_df, king_set)}

    {_build_rule_summary(stage_details, stage_codes, total_stocks)}

    {_build_king_section(king_set, stock_name_map)}"""

    # 主选股
    html += f"""
    <div class="section-card">
        <div class="section-header">
            <h2>主选股结果</h2>
            <span class="badge blue">核心条件：MACD &#8745; ZJTJ</span>
        </div>
        {_build_main_table(result_df, tracker_data, king_set)}
    </div>"""

    # 手工选股
    manual_count = 0
    if manual_df is not None and not manual_df.empty and "core_pass" in manual_df.columns:
        manual_count = int(manual_df["core_pass"].sum())
    elif manual_df is not None and not manual_df.empty:
        manual_count = len(manual_df)

    html += f"""
    <div class="section-card">
        <div class="section-header">
            <h2>手工选股分析</h2>
            <span class="badge green">{manual_count}只满足核心条件</span>
        </div>
        {_build_manual_table(manual_df, tracker_data, king_set)}
    </div>"""

    # 龙头EBK
    ebk_total = 0
    ebk_core = 0
    if ebk_df is not None and not ebk_df.empty:
        ebk_total = len(ebk_df)
        if "core_pass" in ebk_df.columns:
            ebk_core = int(ebk_df["core_pass"].sum())

    html += f"""
    <div class="section-card">
        <div class="section-header">
            <h2>龙头公司EBK分析</h2>
            <span class="badge green">{ebk_core}/{ebk_total}只满足核心条件</span>
        </div>
        {_build_ebk_table(ebk_df, tracker_data, king_set)}
    </div>"""

    # 加分项分布
    if result_df is not None and not result_df.empty and "加分合计" in result_df.columns:
        b2 = int((result_df["加分合计"] == 2).sum())
        b1 = int((result_df["加分合计"] == 1).sum())
        b0 = int((result_df["加分合计"] == 0).sum())
        html += f"""
    <div class="section-card">
        <div class="section-header">
            <h2>加分项分布（主选股）</h2>
        </div>
        <div class="rule-grid">
            <div class="rule-item">
                <div class="rlabel">KDJ + 财务</div>
                <div class="rnum" style="color:#ffd700">{b2}</div>
                <div>2项加分</div>
            </div>
            <div class="rule-item">
                <div class="rlabel">KDJ 或 财务</div>
                <div class="rnum" style="color:#58a6ff">{b1}</div>
                <div>1项加分</div>
            </div>
            <div class="rule-item">
                <div class="rlabel">仅核心条件</div>
                <div class="rnum" style="color:#8b949e">{b0}</div>
                <div>0项加分</div>
            </div>
        </div>
    </div>"""

    # Footer
    html += f"""
    <div class="report-footer">
        <p>A股智能选股系统 &nbsp;|&nbsp; 报告生成时间：{now_str}</p>
        <p style="margin-top:4px;">数据来源：AKShare &nbsp;|&nbsp; 核心规则：MACD买入信号 + ZJTJ庄家控盘</p>
    </div>

</div>
</body>
</html>"""

    return html


def save_html_report(html_content: str, date_str: str, output_dir: str) -> str:
    """保存HTML报告到文件

    参数:
        html_content: HTML 字符串
        date_str: 日期 YYYYMMDD
        output_dir: 输出目录

    返回:
        文件路径
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = f"report_{date_str}.html"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_content)

    return filepath
