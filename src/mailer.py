from __future__ import annotations

import html
import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, List, Optional

from .config import Settings
from .summarizer import SummarizedNews

if TYPE_CHECKING:
    from .feedback import FeedbackInstruction

logger = logging.getLogger(__name__)


class Mailer:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send_digest_to(self, recipient: str, items: List[SummarizedNews], cc: Optional[List[str]] = None, bcc: Optional[List[str]] = None) -> None:
        subject = f"{self.settings.email_subject_prefix} {datetime.now().strftime('%Y-%m-%d')}"
        html_body = self._build_html(items)
        text_body = self._build_text(items)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.settings.smtp_from_name} <{self.settings.smtp_from_email}>"
        msg["To"] = recipient
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        recipients = [recipient] + list(cc or []) + list(bcc or [])
        self._send_message(recipients, msg)
        logger.info("邮件发送成功: %s", recipient)

    def send_feedback_ack(self, recipient: str, instruction: FeedbackInstruction) -> None:
        subject = f"已收到你的反馈 | NGS Daily Digest"
        actions = []
        if instruction.added_rss_feeds or instruction.added_web_pages or instruction.added_api_endpoints:
            actions.append("已记录新增抓取源")
        if instruction.positive_keywords or instruction.negative_keywords:
            actions.append("已更新关键词权重")
        if instruction.summary_style or instruction.summary_max_chars:
            actions.append("已更新摘要风格与长度")
        if instruction.article_feedbacks:
            actions.append("已记录具体新闻条目的点赞/点踩")
        if not actions:
            actions.append("已记录反馈，后续将用于个性化排序与摘要定制")
        details = "\n".join(f"- {item}" for item in actions)
        text_body = (
            "你好，\n\n"
            "系统已收到你的反馈，并将在后续日报中生效。\n"
            f"解析方式：{instruction.parsed_by}\n"
            f"本次生效动作：\n{details}\n\n"
            "感谢你的反馈。"
        )
        html_body = f"""
        <html><body style="font-family:Arial,Helvetica,sans-serif;background:#f7f8fb;padding:24px;">
        <div style="max-width:720px;margin:auto;background:#fff;border-radius:16px;padding:24px;border:1px solid #e5e7eb;">
          <h2 style="margin-top:0;">已收到你的反馈</h2>
          <p>系统会将你的反馈应用到后续新闻抓取、排序和摘要中。</p>
          <p><strong>解析方式：</strong>{html.escape(instruction.parsed_by)}</p>
          <ul>{''.join(f'<li>{html.escape(item)}</li>' for item in actions)}</ul>
          <p style="color:#6b7280;">原始反馈摘要：{html.escape(instruction.raw_text[:300])}</p>
        </div>
        </body></html>
        """
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.settings.smtp_from_name} <{self.settings.smtp_from_email}>"
        msg["To"] = recipient
        msg.attach(MIMEText(text_body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
        self._send_message([recipient], msg)
        logger.info("反馈回执已发送: %s", recipient)

    def _send_message(self, recipients: List[str], msg: MIMEMultipart) -> None:
        logger.info("准备发送邮件至: %s", recipients)
        if self.settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as server:
                server.login(self.settings.smtp_username, self.settings.smtp_password)
                server.sendmail(self.settings.smtp_from_email, recipients, msg.as_string())
        else:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as server:
                server.starttls()
                server.login(self.settings.smtp_username, self.settings.smtp_password)
                server.sendmail(self.settings.smtp_from_email, recipients, msg.as_string())

    def _build_text(self, items: List[SummarizedNews]) -> str:
        if not items:
            content = ["今日未检索到符合条件的 NGS 新闻。"]
        else:
            content = []
            for idx, news in enumerate(items, start=1):
                item = news.item
                content.extend([
                    f"{idx}. {item.title}",
                    f"新闻ID: {item.news_id}",
                    f"来源: {item.source}",
                    f"链接: {item.link}",
                    f"相关度: {item.relevance_score:.2f}",
                    f"摘要: {news.summary}",
                    "",
                ])
        feedback_hint = [
            "反馈方式：直接回复本邮件或发邮件到反馈邮箱。",
            "示例：",
            "- 请增加网站 https://www.nature.com/subjects/sequencing 的抓取",
            "- 更关注单细胞测序和空间转录组",
            "- 摘要太长，请控制在 80 字以内",
            "- 点赞 新闻ID: abc123def456",
            "- 点踩 新闻ID: abc123def456",
            "- 点踩 https://example.com/article",
            "- 点赞标题：A major breakthrough in single-cell sequencing chemistry",
        ]
        return "\n".join(["NGS Daily Digest", "="] + content + [""] + feedback_hint)

    def _build_html(self, items: List[SummarizedNews]) -> str:
        cards = "".join(self._build_card(idx, news) for idx, news in enumerate(items, start=1)) if items else "<p>今日未检索到符合条件的 NGS 新闻。</p>"
        feedback_block = self._build_feedback_block()
        return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" /><title>NGS Daily Digest</title></head>
<body style="margin:0;padding:0;background-color:#f5f7fb;font-family:Arial,Helvetica,sans-serif;color:#1f2937;">
  <div style="max-width:900px;margin:0 auto;padding:24px;">
    <div style="background:#111827;color:#ffffff;border-radius:16px;padding:24px 28px;margin-bottom:20px;">
      <div style="font-size:28px;font-weight:700;">NGS Daily Digest</div>
      <div style="margin-top:8px;font-size:14px;opacity:0.9;">自动新闻抓取 · LLM 智能摘要 · 多用户个性化</div>
      <div style="margin-top:8px;font-size:13px;opacity:0.8;">生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
    </div>
    {feedback_block}
    {cards}
    <div style="font-size:12px;color:#6b7280;padding:12px 4px;">本邮件由 NGS 每日新闻推送系统自动生成。</div>
  </div>
</body></html>"""

    def _build_card(self, idx: int, news: SummarizedNews) -> str:
        item = news.item
        return f"""
<div style="background:#ffffff;border-radius:16px;padding:20px 22px;margin-bottom:16px;border:1px solid #e5e7eb;box-shadow:0 4px 16px rgba(17,24,39,0.04);">
  <div style="font-size:12px;color:#6366f1;font-weight:700;margin-bottom:8px;">#{idx} · {html.escape(item.source)} · Score {item.relevance_score:.2f}</div>
  <div style="font-size:20px;line-height:1.4;font-weight:700;margin-bottom:10px;">{html.escape(item.title)}</div>
  <div style="font-size:13px;color:#6b7280;margin-bottom:10px;">新闻ID：<code>{item.news_id}</code> · 发布时间：{item.published_at.strftime('%Y-%m-%d %H:%M') if item.published_at else '未知'}</div>
  <div style="font-size:15px;line-height:1.8;color:#111827;margin-bottom:16px;">{html.escape(news.summary).replace(chr(10), '<br/>')}</div>
  <a href="{html.escape(item.link)}" style="display:inline-block;padding:10px 14px;background:#111827;color:#ffffff;text-decoration:none;border-radius:10px;font-size:14px;">查看原文</a>
</div>"""

    def _build_feedback_block(self) -> str:
        feedback_address = html.escape(self.settings.feedback_email or self.settings.smtp_from_email)
        return f"""
<div style="background:#eef2ff;border:1px solid #c7d2fe;border-radius:16px;padding:18px 20px;margin-bottom:18px;">
  <div style="font-size:16px;font-weight:700;margin-bottom:10px;color:#312e81;">如何反馈你的偏好</div>
  <div style="font-size:14px;line-height:1.8;color:#3730a3;">
    直接回复本邮件，或发送到：<strong>{feedback_address}</strong><br/>
    你可以这样写：<br/>
    1）请增加网站 https://www.nature.com/subjects/sequencing 的抓取<br/>
    2）更关注单细胞测序、空间转录组和临床测序<br/>
    3）摘要太长，请精简到 80 字以内<br/>
    4）点赞 新闻ID: abc123def456<br/>
    5）点踩 新闻ID: abc123def456<br/>
    6）点赞标题：A major breakthrough in single-cell sequencing chemistry
  </div>
</div>"""
