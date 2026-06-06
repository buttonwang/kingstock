"""A股智能选股系统 - 入口脚本

用法:
    python main.py                          # 默认今天
    python main.py --date 20250101          # 指定日期
    python main.py --force-update           # 强制刷新数据
    python main.py --output result.csv      # 指定输出路径
"""
import sys
import os
import argparse

# 确保项目根目录在 sys.path 中
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pandas as pd
from colorama import init, Fore, Style
from src.stock_selector import StockSelector
from src.stock_tracker import StockTracker
from src.html_reporter import build_html_report, save_html_report
from src.utils import get_trade_date, format_stock_table
from src.email_reporter import send_stock_report, is_email_configured
from src.wechat_pusher import push_stock_report
from config.settings import OUTPUT_PATH, HTML_OUTPUT_PATH


# 初始化 colorama（Windows 兼容）
init(autoreset=True)


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="A股智能选股系统 - 并行筛选（MACD∩ZJTJ为核心，KDJ/财务为加分项）"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="选股日期，YYYYMMDD格式（默认当天）",
    )
    parser.add_argument(
        "--force-update",
        action="store_true",
        default=False,
        help="强制重新拉取数据（忽略缓存）",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="输出文件路径（CSV格式）",
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        default=False,
        help="跳过邮件发送",
    )
    return parser.parse_args()


def print_result(df: pd.DataFrame, date_str: str,
                 stage_details: dict = None):
    """彩色打印选股结果到终端"""
    # 格式化日期显示
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # 标题
    title = f"=== A股智能选股结果 ({display_date}) ==="
    print()
    print(Fore.CYAN + Style.BRIGHT + title)
    print(Fore.CYAN + "=" * len(title))

    # ---- 各规则筛选明细 ----
    if stage_details:
        print()
        print(Fore.WHITE + Style.BRIGHT + "--- 各规则筛选结果（规则2-5并行，核心MACD∩ZJTJ+加分项） ---")
        stage_keys = ["1_RPS板块", "2_MACD", "3_ZJTJ", "4_KDJ", "5_财务"]
        for sk in stage_keys:
            sdf = stage_details.get(sk)
            if sdf is not None and len(sdf) > 1:
                count = len(sdf) - 1  # 减掉表头行
                codes_str = ", ".join(sdf.iloc[1:]["code"].tolist())
                print(f"  {sk}: {count}只  [{codes_str}]")
            else:
                print(f"  {sk}: 0只")
        print()

    if df.empty:
        print()
        print(Fore.YELLOW + Style.BRIGHT + "今日无股票同时满足核心条件(MACD∩ZJTJ)")
        print()
        return

    # 打印数量和说明
    b2 = (df["加分合计"] == 2).sum()
    b1 = (df["加分合计"] == 1).sum()
    b0 = (df["加分合计"] == 0).sum()
    print(Fore.GREEN + f"共选出 {len(df)} 只股票（核心MACD∩ZJTJ）" + Style.RESET_ALL)
    print(Fore.CYAN + f"  加分2项(KDJ+财务): {b2}只 | 加分1项: {b1}只 | 加分0项: {b0}只")
    print()

    # 打印表格
    table = format_stock_table(df)
    if table:
        print(table)

    print()


def _add_tracker_cols(df, tracker_data) -> pd.DataFrame:
    """向DataFrame追加历史追踪列（涨跌幅、连选天数、近3/5/10日涨幅）"""
    if tracker_data is None or df is None or df.empty:
        return df
    result = df.copy()
    result["今日涨跌"] = result["code"].map(
        lambda c: tracker_data.get(str(c).strip(), {}).get("change_pct", None)
    )
    result["连选天数"] = result["code"].map(
        lambda c: tracker_data.get(str(c).strip(), {}).get("consecutive_days", 0)
    )
    result["近3日涨幅"] = result["code"].map(
        lambda c: tracker_data.get(str(c).strip(), {}).get("return_3d", None)
    )
    result["近5日涨幅"] = result["code"].map(
        lambda c: tracker_data.get(str(c).strip(), {}).get("return_5d", None)
    )
    result["近10日涨幅"] = result["code"].map(
        lambda c: tracker_data.get(str(c).strip(), {}).get("return_10d", None)
    )
    # 重点关注标记：连选天数 >= 3
    result["重点关注"] = result["code"].map(
        lambda c: "🌟" if tracker_data.get(str(c).strip(), {}).get("consecutive_days", 0) >= 3 else ""
    )
    return result


def save_result(df: pd.DataFrame, date_str: str, output_path: str = None,
                stage_details: dict = None, tracker_data: dict = None):
    """保存选股结果到CSV和Excel（含各阶段明细Sheet）"""
    os.makedirs(OUTPUT_PATH, exist_ok=True)

    if output_path:
        csv_path = output_path
    else:
        csv_path = os.path.join(OUTPUT_PATH, f"result_{date_str}.csv")

    xlsx_path = csv_path.rsplit(".", 1)[0] + ".xlsx"

    # 追加追踪数据列
    export_df = _add_tracker_cols(df, tracker_data)

    # ---- 导出 CSV（只包含最终结果） ----
    try:
        export_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(Fore.GREEN + f"CSV已导出: {csv_path}")
    except Exception as e:
        print(Fore.RED + f"CSV导出失败: {e}")

    # ---- 导出 Excel（多Sheet，包含各阶段明细） ----
    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            # Sheet 1: 最终结果
            if not export_df.empty:
                export_df.to_excel(writer, sheet_name="最终结果", index=False)
            else:
                pd.DataFrame({"提示": ["今日无股票同时满足核心条件(MACD\u2229ZJTJ)"]}).to_excel(
                    writer, sheet_name="最终结果", index=False
                )

            # 各阶段明细
            if stage_details:
                for sheet_name, sdf in stage_details.items():
                    sdf.to_excel(writer, sheet_name=sheet_name, index=False)

        print(Fore.GREEN + f"Excel已导出: {xlsx_path}（含{len(stage_details or {}) + 1}个Sheet）")
    except ImportError:
        print(Fore.YELLOW + "openpyxl未安装，跳过Excel导出")
    except Exception as e:
        print(Fore.YELLOW + f"Excel导出失败: {e}")


def _display_width(s: str) -> int:
    """计算字符串显示宽度（中文占2）"""
    w = 0
    for ch in str(s):
        if '\u4e00' <= ch <= '\u9fff' or '\u3000' <= ch <= '\u303f':
            w += 2
        else:
            w += 1
    return w


def _pad_to(s: str, target: int) -> str:
    """按显示宽度填充到目标宽度"""
    pad = max(0, target - _display_width(s))
    return s + " " * pad


def _print_analyzer_table(df, title, color=Fore.MAGENTA, king_set=None, tracker_data=None):
    """通用打印分析结果表格（手工选股/龙头EBK共用）"""
    if df is None or df.empty:
        return

    print()
    print(color + Style.BRIGHT + title)
    print(color + "=" * len(title))

    total = len(df)
    core_df = df[df["core_pass"]].copy() if "core_pass" in df.columns else df.copy()
    core_count = len(core_df)

    if core_df.empty:
        print(Fore.YELLOW + f"  共 {total} 只，无股票满足核心条件(MACD∩ZJTJ)")
        print()
        return

    # 列配置（宽度基于显示宽度）
    col_cfg = [
        ("代码", 8),
        ("名称", 8),
        ("MACD", 10),
        ("K", 8),
        ("D", 8),
        ("J", 8),
        ("控盘度", 8),
        ("规则", 24),
        ("今日涨跌", 10),
        ("连板", 6),
        ("近3日", 9),
        ("近5日", 9),
        ("近10日", 9),
    ]

    # 表头
    parts = []
    for hdr, w in col_cfg:
        parts.append(_pad_to(hdr, w))
    print(color + "  " + "  ".join(parts))
    print(color + "  " + "  ".join("-" * w for _, w in col_cfg))

    # 数据行
    for _, row in core_df.iterrows():
        code = str(row.get("code", ""))
        name = str(row.get("name", ""))
        macd_val = row.get("macd", 0)
        k_val = row.get("k", 0)
        d_val = row.get("d", 0)
        j_val = row.get("j", 0)
        kongpan = row.get("kongpan", 0)
        detail = row.get("rule_detail", "")

        # 追踪数据
        td = (tracker_data or {}).get(code, {})
        chg = td.get("change_pct")
        cons = td.get("consecutive_days", 0)
        r3 = td.get("return_3d")
        r5 = td.get("return_5d")
        r10 = td.get("return_10d")

        chg_str = f"{chg:+6.2f}%" if chg is not None else "  --  "
        cons_str = f"{cons:>3d}" if cons > 0 else "  - "
        r3_str = f"{r3:+7.2f}%" if r3 is not None else "   --   "
        r5_str = f"{r5:+7.2f}%" if r5 is not None else "   --   "
        r10_str = f"{r10:+8.2f}%" if r10 is not None else "   --    "

        if len(detail) > 24:
            detail = detail[:22] + ".."

        is_king = king_set and code in king_set
        clr = Fore.YELLOW + Style.BRIGHT if is_king else Fore.GREEN

        cells = [
            _pad_to(code, 8),
            _pad_to(name, 8),
            _pad_to(f"{macd_val:+10.4f}", 10),
            _pad_to(f"{k_val:8.2f}", 8),
            _pad_to(f"{d_val:8.2f}", 8),
            _pad_to(f"{j_val:8.2f}", 8),
            _pad_to(f"{kongpan:8.2f}", 8),
            detail,
            chg_str,
            cons_str,
            r3_str,
            r5_str,
            r10_str,
        ]
        print(clr + "  " + "  ".join(cells))

    print()
    if king_set:
        king_in_this = [c for c in king_set if c in core_df["code"].values]
        if king_in_this:
            print(Fore.YELLOW + Style.BRIGHT + "  👑 King Stock — 金色标注（在三部分中都出现的股票）")
    print(Fore.CYAN + f"共 {total} 只，其中 {core_count} 只满足核心条件(MACD∩ZJTJ)")
    print()


def find_king_stocks(result_df, manual_df, ebk_df) -> set:
    """找出在三个选股部分中都出现的股票代码（King Stock）"""
    sets = []
    if result_df is not None and not result_df.empty and "code" in result_df.columns:
        sets.append(set(result_df["code"].tolist()))
    if manual_df is not None and not manual_df.empty and "code" in manual_df.columns:
        sets.append(set(manual_df[manual_df["core_pass"]]["code"].tolist()))
    if ebk_df is not None and not ebk_df.empty and "code" in ebk_df.columns:
        sets.append(set(ebk_df[ebk_df["core_pass"]]["code"].tolist()))

    if len(sets) < 2:
        return set()
    # 所有集合的交集
    king = sets[0]
    for s in sets[1:]:
        king = king & s
    return king


def print_manual_result(manual_df: pd.DataFrame, king_set: set = None, tracker_data: dict = None):
    """彩色打印手工选股分析结果（表格格式）"""
    _print_analyzer_table(
        manual_df,
        title="=== 手工选股分析报告 ===",
        color=Fore.MAGENTA,
        king_set=king_set,
        tracker_data=tracker_data,
    )


def save_manual_result(manual_df: pd.DataFrame, date_str: str, tracker_data: dict = None):
    """保存手工选股分析结果到文件"""
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    csv_path = os.path.join(OUTPUT_PATH, f"manual_{date_str}.csv")

    # 导出列（去掉布尔列，保留可读列）
    export_cols = [
        "code", "name", "source", "rule_detail",
        "dif", "dea", "macd", "kongpan", "k", "d", "j",
        "macd_pass", "zjtj_pass", "kdj_pass", "finance_pass", "core_pass",
    ]
    export_df = manual_df[[c for c in export_cols if c in manual_df.columns]].copy()

    # 布尔列转为中文标记
    bool_map = {True: "\u2713", False: "\u2717"}
    for col in ["macd_pass", "zjtj_pass", "kdj_pass", "finance_pass", "core_pass"]:
        if col in export_df.columns:
            label_map = {
                "macd_pass": "MACD\u4e70\u5165",
                "zjtj_pass": "ZJTJ\u63a7\u76d8",
                "kdj_pass": "KDJ\u4e70\u5165",
                "finance_pass": "\u8d22\u52a1\u589e\u957f",
                "core_pass": "\u6838\u5fc3\u6ee1\u8db3",
            }
            export_df = export_df.rename(columns={col: label_map.get(col, col)})
            export_df[label_map.get(col, col)] = export_df[label_map.get(col, col)].map(bool_map)

    # 追加追踪列
    export_df = _add_tracker_cols(export_df, tracker_data)

    try:
        export_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(Fore.GREEN + f"\u624b\u5de5\u9009\u80a1CSV\u5df2\u5bfc\u51fa: {csv_path}")
    except Exception as e:
        print(Fore.YELLOW + f"\u624b\u5de5\u9009\u80a1CSV\u5bfc\u51fa\u5931\u8d25: {e}")

    # \u8ffd\u52a0\u5230Excel
    xlsx_path = csv_path.rsplit(".", 1)[0] + ".xlsx"
    try:
        export_df.to_excel(xlsx_path, index=False, sheet_name="\u624b\u5de5\u9009\u80a1")
        print(Fore.GREEN + f"\u624b\u5de5\u9009\u80a1Excel\u5df2\u5bfc\u51fa: {xlsx_path}")
    except Exception as e:
        print(Fore.YELLOW + f"\u624b\u5de5\u9009\u80a1Excel\u5bfc\u51fa\u5931\u8d25: {e}")


def print_ebk_result(ebk_df: pd.DataFrame, king_set: set = None, tracker_data: dict = None):
    """彩色打印龙头公司EBK分析结果（表格格式，完整显示所有满足核心条件的股票）"""
    _print_analyzer_table(
        ebk_df,
        title="=== 龙头公司EBK分析报告 ===",
        color=Fore.BLUE,
        king_set=king_set,
        tracker_data=tracker_data,
    )


def save_ebk_result(ebk_df: pd.DataFrame, date_str: str, tracker_data: dict = None):
    """保存龙头公司EBK分析结果到文件"""
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    csv_path = os.path.join(OUTPUT_PATH, f"ebk_{date_str}.csv")

    # 导出列
    export_cols = [
        "code", "name", "source", "rule_detail",
        "dif", "dea", "macd", "kongpan", "k", "d", "j",
        "macd_pass", "zjtj_pass", "kdj_pass", "finance_pass", "core_pass",
    ]
    export_df = ebk_df[[c for c in export_cols if c in ebk_df.columns]].copy()

    # 布尔列转为中文标记
    bool_map = {True: "✓", False: "✗"}
    for col in ["macd_pass", "zjtj_pass", "kdj_pass", "finance_pass", "core_pass"]:
        if col in export_df.columns:
            label_map = {
                "macd_pass": "MACD买入",
                "zjtj_pass": "ZJTJ控盘",
                "kdj_pass": "KDJ买入",
                "finance_pass": "财务增长",
                "core_pass": "核心满足",
            }
            export_df = export_df.rename(columns={col: label_map.get(col, col)})
            export_df[label_map.get(col, col)] = export_df[label_map.get(col, col)].map(bool_map)

    # 追加追踪列
    export_df = _add_tracker_cols(export_df, tracker_data)

    try:
        export_df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(Fore.GREEN + f"龙头公司EBK CSV已导出: {csv_path}")
    except Exception as e:
        print(Fore.YELLOW + f"龙头公司EBK CSV导出失败: {e}")

    xlsx_path = csv_path.rsplit(".", 1)[0] + ".xlsx"
    try:
        export_df.to_excel(xlsx_path, index=False, sheet_name="龙头公司EBK")
        print(Fore.GREEN + f"龙头公司EBK Excel已导出: {xlsx_path}")
    except Exception as e:
        print(Fore.YELLOW + f"龙头公司EBK Excel导出失败: {e}")


def save_combined_result(result_df, manual_df, ebk_df, date_str, tracker_data=None):
    """将三部分选股结果合并保存到同一CSV和Excel文件"""
    os.makedirs(OUTPUT_PATH, exist_ok=True)
    csv_path = os.path.join(OUTPUT_PATH, f"combined_{date_str}.csv")
    xlsx_path = csv_path.rsplit(".", 1)[0] + ".xlsx"

    all_parts = []

    def _prepare_part(df, source_label):
        if df is None or df.empty:
            return None
        part = df.copy()
        part["来源"] = source_label
        if "core_pass" in part.columns:
            part = part[part["core_pass"]].copy()
        part = _add_tracker_cols(part, tracker_data)
        return part

    parts = [
        _prepare_part(result_df, "主选股"),
        _prepare_part(manual_df, "手工选股"),
        _prepare_part(ebk_df, "龙头EBK"),
    ]

    for p in parts:
        if p is not None:
            all_parts.append(p)

    if not all_parts:
        return

    combined = pd.concat(all_parts, ignore_index=True)

    # 保存合并CSV
    try:
        combined.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(Fore.GREEN + f"合并报告CSV已导出: {csv_path}")
    except Exception as e:
        print(Fore.YELLOW + f"合并报告CSV导出失败: {e}")

    # 保存合并Excel（多Sheet）
    try:
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            # Sheet: 全部
            combined.to_excel(writer, sheet_name="全部汇总", index=False)
            # 按来源分Sheet
            for label, p in zip(["主选股", "手工选股", "龙头EBK"], parts):
                if p is not None and not p.empty:
                    p.to_excel(writer, sheet_name=label, index=False)
        print(Fore.GREEN + f"合并报告Excel已导出: {xlsx_path}")
    except ImportError:
        print(Fore.YELLOW + "openpyxl未安装，跳过合并Excel导出")
    except Exception as e:
        print(Fore.YELLOW + f"合并报告Excel导出失败: {e}")


def main():
    """主入口"""
    args = parse_args()

    # 确定选股日期
    try:
        date_str = get_trade_date(args.date)
    except ValueError as e:
        print(Fore.RED + f"日期错误: {e}")
        sys.exit(1)

    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    print(Fore.CYAN + f"选股日期: {display_date}")
    if args.force_update:
        print(Fore.YELLOW + "模式: 强制刷新数据（忽略缓存）")
    print()

    # 执行选股
    try:
        selector = StockSelector(force_update=args.force_update)
        result_df = selector.run(date=date_str)
    except KeyboardInterrupt:
        print(Fore.YELLOW + "\n用户中断选股流程")
        sys.exit(0)
    except ConnectionError as e:
        print(Fore.RED + f"网络连接异常: {e}")
        print(Fore.RED + "请检查网络连接后重试")
        sys.exit(1)
    except Exception as e:
        print(Fore.RED + f"选股过程出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 获取各阶段明细
    all_stocks = selector.fetcher.get_all_stocks()
    stock_name_map = dict(zip(all_stocks["code"], all_stocks["name"]))
    stage_details = selector.build_stage_details(stock_name_map)

    # 找出King Stock（在三部分中都出现的股票）
    manual_df = getattr(selector, "manual_result", None)
    ebk_df = getattr(selector, "ebk_result", None)
    king_set = find_king_stocks(result_df, manual_df, ebk_df)
    if king_set:
        king_names = [f"{c}({stock_name_map.get(c, '')})" for c in king_set]
        print(Fore.YELLOW + Style.BRIGHT + f"King Stock: {', '.join(king_names)} 在三部分选股中均满足核心条件")

    # ---- 历史追踪：记录选股结果 + 计算涨跌幅/连板天数 ----
    tracker = StockTracker()
    tracker.record_results(result_df, manual_df, ebk_df, date_str)
    tracker_data = tracker.compute_all_returns(
        selector.fetcher, result_df, manual_df, ebk_df, date_str,
    )

    # ---- 生成并保存HTML报告 ----
    try:
        html_content = build_html_report(
            date_str=date_str,
            result_df=result_df,
            stage_details=stage_details,
            stage_codes=selector.stage_codes,
            total_stocks=len(all_stocks),
            manual_df=manual_df,
            ebk_df=ebk_df,
            king_set=king_set,
            stock_name_map=stock_name_map,
            tracker_data=tracker_data,
        )
        html_path = save_html_report(html_content, date_str, HTML_OUTPUT_PATH)
        # 保存HTML到数据库
        tracker.save_html_report(date_str, html_content)
        print(Fore.GREEN + f"HTML报告已生成: {html_path}")
    except Exception as e:
        print(Fore.YELLOW + f"HTML报告生成失败: {e}")
        html_path = None

    tracker.close()

    # 输出结果（三部分）
    print_result(result_df, date_str, stage_details)

    # 输出手工选股分析结果（表格格式，含涨跌幅/连板天数）
    print_manual_result(manual_df, king_set, tracker_data)

    # 输出龙头公司EBK分析结果（表格格式，含涨跌幅/连板天数）
    print_ebk_result(ebk_df, king_set, tracker_data)

    # 输出King Stock汇总
    if king_set:
        print()
        print(Fore.YELLOW + Style.BRIGHT + "=" * 50)
        print(Fore.YELLOW + Style.BRIGHT + "  👑 KING STOCK 汇总 👑")
        print(Fore.YELLOW + Style.BRIGHT + "=" * 50)
        print(Fore.WHITE + "  以下股票在「主选股 + 手工选股 + 龙头EBK」三部分中都满足核心条件：")
        for code in sorted(king_set):
            name = stock_name_map.get(code, "")
            print(Fore.YELLOW + Style.BRIGHT + f"    {code} {name}")
        print()

    # 保存结果
    save_result(result_df, date_str, args.output, stage_details, tracker_data)

    # 保存手工选股分析结果
    if manual_df is not None and not manual_df.empty:
        save_manual_result(manual_df, date_str, tracker_data)

    # 保存龙头公司EBK分析结果
    if ebk_df is not None and not ebk_df.empty:
        save_ebk_result(ebk_df, date_str, tracker_data)

    # 保存合并报告（三部分合一）
    save_combined_result(result_df, manual_df, ebk_df, date_str, tracker_data)

    # 发送邮件报告
    if not args.no_email and is_email_configured():
        csv_path = args.output or os.path.join(OUTPUT_PATH, f"result_{date_str}.csv")
        xlsx_path = csv_path.rsplit(".", 1)[0] + ".xlsx"
        all_stocks_count = len(all_stocks)
        send_stock_report(
            df=result_df,
            date_str=date_str,
            stage_details=stage_details,
            stage_codes=selector.stage_codes,
            total_stocks=all_stocks_count,
            csv_path=csv_path if os.path.exists(csv_path) else None,
            xlsx_path=xlsx_path if os.path.exists(xlsx_path) else None,
            manual_df=manual_df,
            ebk_df=ebk_df,
            king_set=king_set,
            stock_name_map=stock_name_map,
            html_path=html_path,
            tracker_data=tracker_data,
        )
    elif not args.no_email:
        print(Fore.YELLOW + "邮件未配置，跳过报告发送（需在config/settings.py中配置）")

    # 推送到微信
    manual_core = 0
    if manual_df is not None and not manual_df.empty:
        manual_core = manual_df["core_pass"].sum()
    ebk_core = 0
    if ebk_df is not None and not ebk_df.empty:
        ebk_core = ebk_df["core_pass"].sum()
    push_stock_report(
        date_str=date_str,
        result_count=len(result_df),
        manual_count=int(manual_core),
        ebk_count=int(ebk_core),
    )

    # 关闭数据库连接
    selector.fetcher.close()

    return result_df


if __name__ == "__main__":
    main()
