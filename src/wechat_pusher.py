"""微信推送 - 通过 PushPlus 将选股结果推送到微信公众号"""

import logging
import requests
from config.settings import PUSHPLUS_TOKEN

logger = logging.getLogger("wechat_pusher")


def push_stock_report(
    date_str: str,
    result_count: int,
    manual_count: int,
    ebk_count: int,
    summary_text: str = None,
) -> bool:
    """推送选股结果到微信（PushPlus）

    参数:
        date_str: 日期 YYYYMMDD
        result_count: 核心选出股票数
        manual_count: 手工选股满足核心条件数
        ebk_count: EBK龙头满足核心条件数
        summary_text: 简要汇总文本

    返回:
        是否推送成功
    """
    if not PUSHPLUS_TOKEN:
        logger.info("PUSHPLUS_TOKEN 未配置，跳过微信推送")
        return False

    display_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # 构建标题
    parts = []
    if result_count > 0:
        parts.append(f"核心{result_count}只")
    if manual_count > 0:
        parts.append(f"手工{manual_count}只")
    if ebk_count > 0:
        parts.append(f"EBK{ebk_count}只")

    title = f"智能选股 {display_date} | {'/'.join(parts) if parts else '无信号'}"

    # 构建内容（纯文本，微信中阅读更友好）
    lines = [f"📊 A股智能选股报告 ({display_date})"]
    lines.append("=" * 30)

    if result_count > 0:
        lines.append(f"\n🎯 核心选出: {result_count} 只")
        lines.append(f"    详见公众号菜单或邮件附件")
    else:
        lines.append(f"\n📭 今日无符合条件的核心股票")

    if manual_count > 0:
        lines.append(f"\n✋ 手工选股满足条件: {manual_count} 只")
    if ebk_count > 0:
        lines.append(f"\n🏢 龙头EBK满足条件: {ebk_count} 只")

    if summary_text:
        lines.append(f"\n{summary_text}")

    lines.append(f"\n---")
    lines.append(f"报告时间: {display_date}")
    lines.append(f"完整报告请查看邮件附件")

    content = "\n".join(lines)
    content = content.replace(" ", "&nbsp;")  # 保留缩进格式

    return _push(title, content, template="txt")


def _push(title: str, content: str, template: str = "txt") -> bool:
    """发送到 PushPlus API"""
    try:
        resp = requests.post(
            "http://www.pushplus.plus/send",
            json={
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": content,
                "template": template,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 200:
            logger.info("微信推送成功: %s", title)
            return True
        else:
            logger.error("微信推送失败: %s", data.get("msg", resp.text))
            return False
    except requests.RequestException as e:
        logger.error("微信推送请求异常: %s", e)
        return False
