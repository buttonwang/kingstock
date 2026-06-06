"""选股结果邮件报告 - 自动生成分析报告并发送邮件"""

import smtplib
import os
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime
from email.utils import formatdate
import pandas as pd

from config.settings import (
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD,
    EMAIL_RECIPIENTS, EMAIL_FROM_NAME,
)
from src.utils import setup_logging

logger = setup_logging("email_reporter")


def is_email_configured() -> bool:
    """检查邮件配置是否完整"""
    return bool(SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASSWORD and EMAIL_RECIPIENTS)


def build_report_text(
    df: pd.DataFrame, date_str: str, stage_details: dict = None,
    stage_codes: dict = None, total_stocks: int = 0,
    manual_df: pd.DataFrame = None,
    ebk_df: pd.DataFrame = None,
    king_set: set = None,
    stock_name_map: dict = None,
) -> str:
    """构建分析报告文本

    参数:
        df: 最终结果DataFrame
        date_str: 日期 YYYYMMDD
        stage_details: 各阶段明细
        stage_codes: 各阶段股票代码集合
        total_stocks: 初始股票总数
    """
    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    lines = []
    sep = "=" * 50

    # ---- 标题 ----
    lines.append(sep)
    lines.append(f"  A股智能选股分析报告 ({display_date})")
    lines.append(sep)
    lines.append("")

    # ---- 各规则筛选结果 ----
    lines.append("【各规则筛选结果】")
    lines.append("-" * 40)

    rule_config = {
        "1_RPS板块": ("规则1 板块RPS", "初始池"),
        "2_MACD":    ("规则2 MACD买入信号", "核心"),
        "3_ZJTJ":    ("规则3 ZJTJ庄家控盘", "核心"),
        "4_KDJ":     ("规则4 KDJ买入信号", "加分项"),
        "5_财务":    ("规则5 财务基本面", "加分项"),
    }

    for stage_key, (label, rtype) in rule_config.items():
        codes = stage_codes.get(stage_key, set()) if stage_codes else set()
        if stage_key == "1_RPS板块":
            input_count = total_stocks
        else:
            pool = stage_codes.get("1_RPS板块", set()) if stage_codes else set()
            input_count = len(pool)
        lines.append(f"  {label:20s}  {input_count}只 → {len(codes):3d}只  [{rtype}]")

    lines.append("")

    # ---- 核心结果 ----
    if df.empty:
        lines.append("【最终结果】")
        lines.append("  今日无股票同时满足核心条件(MACD ∩ ZJTJ)")
        lines.append("")
        return "\n".join(lines)

    core_codes = stage_codes.get("全部规则", set()) if stage_codes else set()
    lines.append(f"【最终结果】MACD ∩ ZJTJ = {len(core_codes)} 只核心股票")
    lines.append("")

    # ---- 加分分布 ----
    b2 = (df["加分合计"] == 2).sum()
    b1 = (df["加分合计"] == 1).sum()
    b0 = (df["加分合计"] == 0).sum()
    lines.append("【加分项分布】")
    lines.append(f"  🏆 加分2项（KDJ+财务）:  {b2}只")
    lines.append(f"  ✅ 加分1项:              {b1}只")
    lines.append(f"  ➖ 加分0项（仅核心）:    {b0}只")
    lines.append("")

    # ---- 核心股票详情 ----
    lines.append("【核心股票详情】")
    lines.append("-" * 80)
    header = f"{'代码':>8}  {'名称':<8}  {'板块':<10}  {'MACD':>8}  {'控盘度':>6}  {'KDJ':>4}  {'财务':>4}"
    lines.append(header)
    lines.append("-" * 80)

    for _, row in df.iterrows():
        macd_val = row.get("macd", 0)
        macd_str = f"{macd_val:+.4f}" if isinstance(macd_val, (int, float)) else str(macd_val)
        kongpan = row.get("kongpan", 0)
        kongpan_str = f"{kongpan:.2f}" if isinstance(kongpan, (int, float)) else str(kongpan)
        kdj_mark = row.get("KDJ加分", "")
        fin_mark = row.get("财务加分", "")

        lines.append(
            f"{str(row.get('code', '')):>8}  "
            f"{str(row.get('name', '')):<8}  "
            f"{str(row.get('sector', '')):<10}  "
            f"{macd_str:>8}  "
            f"{kongpan_str:>6}  "
            f"{'✓' if kdj_mark else '':>4}  "
            f"{'✓' if fin_mark else '':>4}"
        )

    lines.append("-" * 80)
    lines.append("")

    # ---- 亮点提示 ----
    bonus1_df = df[df["加分合计"] >= 1]
    if not bonus1_df.empty:
        lines.append("【⭐ 亮点提示】")
        for _, row in bonus1_df.iterrows():
            parts = [f"{row.get('name', '')}({row.get('code', '')})"]
            if row.get("KDJ加分", ""):
                parts.append("满足KDJ")
            if row.get("财务加分", ""):
                parts.append("满足财务条件")
            lines.append(f"  {row.get('code', '')} - " + "、".join(parts))
        lines.append("")

    lines.append(sep)
    lines.append(f"  报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(sep)

    # ---- 手工选股分析报告 ----
    if manual_df is not None and not manual_df.empty:
        lines.append("")
        lines.append("=" * 50)
        lines.append("  手工选股分析报告")
        lines.append("=" * 50)
        lines.append("")

        core_manual = manual_df[manual_df["core_pass"]]
        lines.append(f"手工选股共 {len(manual_df)} 只，其中 {len(core_manual)} 只满足核心条件(MACD∩ZJTJ)")
        lines.append("")

        for _, row in manual_df.iterrows():
            code = row.get("code", "")
            name = row.get("name", "")
            source = row.get("source", "")
            detail = row.get("rule_detail", "")
            is_core = "✓核心满足" if row.get("core_pass") else "✗未满足核心"

            lines.append(f"  {code} {name} [{source}]")
            lines.append(f"    规则: {detail}  →  {is_core}")

            if row.get("core_pass"):
                macd_val = row.get("macd", 0)
                kongpan_val = row.get("kongpan", 0)
                k_val = row.get("k", 0)
                d_val = row.get("d", 0)
                j_val = row.get("j", 0)
                lines.append(
                    f"    MACD={macd_val:+.4f}  控盘={kongpan_val:.2f}  "
                    f"K={k_val:.1f} D={d_val:.1f} J={j_val:.1f}"
                )
            lines.append("")

    # ---- 龙头公司EBK分析报告 ----
    if ebk_df is not None and not ebk_df.empty:
        lines.append("")
        lines.append("=" * 50)
        lines.append("  龙头公司EBK分析报告")
        lines.append("=" * 50)
        lines.append("")

        core_ebk = ebk_df[ebk_df["core_pass"]]
        lines.append(f"龙头公司EBK共 {len(ebk_df)} 只，其中 {len(core_ebk)} 只满足核心条件(MACD∩ZJTJ)")
        lines.append("")

        if not core_ebk.empty:
            for _, row in core_ebk.iterrows():
                code = row.get("code", "")
                name = row.get("name", "")
                detail = row.get("rule_detail", "")
                lines.append(f"  {code} {name}")
                lines.append(f"    规则: {detail}  →  ✓核心满足")
                macd_val = row.get("macd", 0)
                kongpan_val = row.get("kongpan", 0)
                k_val = row.get("k", 0)
                d_val = row.get("d", 0)
                j_val = row.get("j", 0)
                lines.append(
                    f"    MACD={macd_val:+.4f}  控盘={kongpan_val:.2f}  "
                    f"K={k_val:.1f} D={d_val:.1f} J={j_val:.1f}"
                )
                lines.append("")

    # ---- King Stock 汇总 ----
    if king_set:
        lines.append("")
        lines.append("=" * 50)
        lines.append("  👑 KING STOCK 汇总 👑")
        lines.append("=" * 50)
        lines.append("以下股票在「主选股 + 手工选股 + 龙头EBK」三部分中都满足核心条件：")
        lines.append("")
        for code in sorted(king_set):
            name = stock_name_map.get(code, "") if stock_name_map else ""
            lines.append(f"  ★ {code} {name}")
        lines.append("")
        lines.append("=" * 50)

    return "\n".join(lines)


def send_email(
    subject: str,
    body_text: str,
    attachments: list = None,
    is_html: bool = False,
) -> bool:
    """发送邮件

    参数:
        subject: 邮件主题
        body_text: 邮件正文（纯文本）
        attachments: 附件文件路径列表

    返回:
        是否发送成功
    """
    if not is_email_configured():
        logger.warning("邮件未配置，跳过发送")
        return False

    msg = MIMEMultipart()
    msg["From"] = f"{EMAIL_FROM_NAME} <{SMTP_USER}>"
    msg["To"] = ", ".join(EMAIL_RECIPIENTS)
    msg["Subject"] = subject

    # 邮件头：添加标准邮件头减少被识别为垃圾邮件的概率
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = f"<stock-{datetime.now().strftime('%Y%m%d%H%M%S%f')}-{os.getpid()}@stock-selecter>"
    msg["MIME-Version"] = "1.0"
    msg["Precedence"] = "bulk"
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-Mailer"] = "A股智能选股系统"

    # 正文：使用 multipart/alternative 同时提供纯文本和HTML
    if is_html:
        # 解析HTML后提取纯文本作为备选
        import re
        text_plain = re.sub(r"<[^>]+>", "", body_text)
        text_plain = re.sub(r"\s+", " ", text_plain).strip()
        msg.attach(MIMEText(text_plain, "plain", "utf-8"))
        msg.attach(MIMEText(body_text, "html", "utf-8"))
    else:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

    # 附件
    if attachments:
        for file_path in attachments:
            if not os.path.exists(file_path):
                logger.warning("附件不存在，跳过: %s", file_path)
                continue
            with open(file_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                filename = os.path.basename(file_path)
                part.add_header(
                    "Content-Disposition",
                    f"attachment; filename={filename}",
                )
                msg.attach(part)

    # 发送（含自动重试 + 延迟重试应对163临时风控）
    max_retries = 2
    delayed_retry_minutes = 15
    quick_retries_exhausted = False

    for attempt in range(1 + max_retries):
        try:
            logger.info("正在发送邮件到 %s ...（第%d次）",
                        ", ".join(EMAIL_RECIPIENTS), attempt + 1)
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, EMAIL_RECIPIENTS, msg.as_string())

            logger.info("邮件发送成功")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error("邮件认证失败，请检查SMTP_USER和SMTP_PASSWORD是否正确")
            return False  # 认证失败无需重试

        except smtplib.SMTPException as e:
            logger.error("邮件发送失败 (SMTP) 第%d次: %s", attempt + 1, e)
            if attempt < max_retries:
                wait = 3 * (attempt + 1)
                logger.info("等待 %d 秒后重试...", wait)
                time.sleep(wait)
            else:
                logger.error("邮件发送已重试 %d 次，全部失败", max_retries)
                quick_retries_exhausted = True

        except Exception as e:
            logger.error("邮件发送失败: %s", e)
            if attempt < max_retries:
                wait = 3 * (attempt + 1)
                logger.info("等待 %d 秒后重试...", wait)
                time.sleep(wait)
            else:
                quick_retries_exhausted = True

    # 快速重试全部失败 → 可能是163临时风控，等15分钟再试一次
    if quick_retries_exhausted:
        logger.info("快速重试全部失败，等待 %d 分钟后延迟重试...", delayed_retry_minutes)
        time.sleep(delayed_retry_minutes * 60)
        try:
            logger.info("正在发送邮件到 %s ...（延迟重试）",
                        ", ".join(EMAIL_RECIPIENTS))
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, EMAIL_RECIPIENTS, msg.as_string())
            logger.info("邮件发送成功（延迟重试）")
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error("延迟重试认证失败")
            return False
        except Exception as e:
            logger.error("延迟重试也失败: %s", e)
            return False

    return False


def build_report_html(
    df: pd.DataFrame, date_str: str,
    stage_details: dict = None,
    stage_codes: dict = None,
    total_stocks: int = 0,
    manual_df: pd.DataFrame = None,
    ebk_df: pd.DataFrame = None,
    king_set: set = None,
    stock_name_map: dict = None,
    tracker_data: dict = None,
) -> str:
    """构建HTML格式的分析报告"""
    from src.html_reporter import build_html_report
    return build_html_report(
        date_str=date_str,
        result_df=df,
        stage_details=stage_details,
        stage_codes=stage_codes,
        total_stocks=total_stocks,
        manual_df=manual_df,
        ebk_df=ebk_df,
        king_set=king_set,
        stock_name_map=stock_name_map,
        tracker_data=tracker_data,
    )


def send_stock_report(
    df: pd.DataFrame, date_str: str,
    stage_details: dict = None,
    stage_codes: dict = None,
    total_stocks: int = 0,
    csv_path: str = None,
    xlsx_path: str = None,
    manual_df: pd.DataFrame = None,
    ebk_df: pd.DataFrame = None,
    king_set: set = None,
    stock_name_map: dict = None,
    html_path: str = None,
    tracker_data: dict = None,
) -> bool:
    """发送选股分析报告邮件（一站式接口）

    参数:
        df: 最终结果DataFrame
        date_str: 日期 YYYYMMDD
        stage_details: 各阶段明细
        stage_codes: 各阶段股票代码集合
        total_stocks: 初始股票总数
        csv_path: CSV附件路径
        xlsx_path: Excel附件路径
        html_path: HTML报告文件路径（作为邮件正文HTML）
        tracker_data: 涨跌幅和连板天数数据

    返回:
        是否发送成功
    """
    if not is_email_configured():
        logger.info("邮件未配置，跳过报告发送")
        return False

    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # 构建报告正文（优先使用HTML）
    body = None
    is_html = False
    if html_path and os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            body = f.read()
        is_html = True
        logger.info("使用HTML报告作为邮件正文: %s", html_path)
    else:
        body = build_report_text(
            df, date_str, stage_details, stage_codes, total_stocks,
            manual_df, ebk_df, king_set, stock_name_map,
        )
        is_html = False

    # 确定主题
    manual_core_count = 0
    if manual_df is not None and not manual_df.empty:
        manual_core_count = manual_df["core_pass"].sum()

    ebk_core_count = 0
    if ebk_df is not None and not ebk_df.empty:
        ebk_core_count = ebk_df["core_pass"].sum()

    king_prefix = "King " if king_set else ""
    if not df.empty or manual_core_count > 0 or ebk_core_count > 0:
        parts = []
        if king_set:
            parts.append(f"{len(king_set)}只King")
        if not df.empty:
            parts.append(f"选出 {len(df)} 只核心")
        if manual_core_count > 0:
            parts.append(f"手工 {manual_core_count} 只")
        if ebk_core_count > 0:
            parts.append(f"龙头EBK {ebk_core_count} 只")
        subject = f"[{king_prefix}智能选股] {display_date} - {'，'.join(parts)}"
    else:
        subject = f"[智能选股] {display_date} - 今日无符合条件的股票"

    # 附件
    attachments = []
    for fp in [csv_path, xlsx_path]:
        if fp and os.path.exists(fp):
            attachments.append(fp)

    return send_email(subject, body, attachments, is_html=is_html)
