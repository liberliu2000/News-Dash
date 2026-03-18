from __future__ import annotations

import email
import imaplib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr
from pathlib import Path
from typing import List, Optional

import requests

from .config import Settings
from .fetcher import NewsStateStore
from .mailer import Mailer
from .profile_manager import UserProfile, UserProfileStore

logger = logging.getLogger(__name__)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
KEYWORD_BLOCK_RE = re.compile(r"(?:关注|关键词|interest|focus|keyword)[：:]+(.+)", re.IGNORECASE)
SOURCE_BLOCK_RE = re.compile(r"(?:增加来源|新增来源|添加来源|rss|source|feed)[：:]+(.+)", re.IGNORECASE)
SUMMARY_LENGTH_RE = re.compile(r"(?:摘要|summary).{0,8}(太长|太短|精简|简短|详细|展开)", re.IGNORECASE)
NEGATIVE_RE = re.compile(r"(?:不相关|irrelevant|不要|减少|少看|not relevant)", re.IGNORECASE)
POSITIVE_RE = re.compile(r"(?:喜欢|相关|很好|不错|more like this|relevant)", re.IGNORECASE)
MAX_CHAR_RE = re.compile(r"(?:不超过|最多|within)\s*(\d{2,4})\s*(?:字|字符|chars?)", re.IGNORECASE)
NEWS_ID_RE = re.compile(r"(?:新闻ID|News ID|ID|编号)\s*[:：#]?\s*([a-f0-9]{6,16})", re.IGNORECASE)
LIKE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [r"点赞", r"like\b", r"thumbs?\s*up", r"推荐"]]
DISLIKE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [r"点踩", r"dislike\b", r"thumbs?\s*down", r"不喜欢", r"不相关"]]
TITLE_FEEDBACK_RE = re.compile(r"(?:点赞标题|点踩标题|like title|dislike title|标题)\s*[:：]\s*(.+)", re.IGNORECASE)


@dataclass
class ArticleFeedbackAction:
    article_fingerprint: Optional[str] = None
    article_link: Optional[str] = None
    article_title: Optional[str] = None
    article_id: Optional[str] = None
    vote: int = 0
    reason: str = ""


@dataclass
class FeedbackInstruction:
    sender: str
    subject: str
    received_at: str
    raw_text: str
    message_id: str = ""
    added_rss_feeds: List[str] = field(default_factory=list)
    added_web_pages: List[str] = field(default_factory=list)
    added_api_endpoints: List[str] = field(default_factory=list)
    positive_keywords: List[str] = field(default_factory=list)
    negative_keywords: List[str] = field(default_factory=list)
    preferred_sources: List[str] = field(default_factory=list)
    disliked_sources: List[str] = field(default_factory=list)
    summary_style: Optional[str] = None
    summary_max_chars: Optional[int] = None
    summary_focus: List[str] = field(default_factory=list)
    satisfaction: Optional[str] = None
    article_feedbacks: List[ArticleFeedbackAction] = field(default_factory=list)
    parsed_by: str = "rule"


class FeedbackLogger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, instruction: FeedbackInstruction) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(instruction), ensure_ascii=False) + "\n")


class BaseFeedbackParser:
    def __init__(self, settings: Settings):
        self.settings = settings

    def parse(self, sender: str, subject: str, text: str, message_id: str = "") -> FeedbackInstruction:
        raise NotImplementedError


class RuleFeedbackParser(BaseFeedbackParser):
    def parse(self, sender: str, subject: str, text: str, message_id: str = "") -> FeedbackInstruction:
        normalized_text = self._clean_reply_text(text)
        instruction = FeedbackInstruction(
            sender=sender,
            subject=subject,
            received_at=datetime.now(timezone.utc).isoformat(),
            raw_text=normalized_text,
            message_id=message_id,
            parsed_by="rule",
        )

        urls = URL_RE.findall(normalized_text)
        for url in urls:
            lower = url.lower()
            if "rss" in lower or lower.endswith(".xml") or "feed" in lower:
                instruction.added_rss_feeds.append(url)
            elif "api" in lower or "newsapi" in lower or "gnews" in lower:
                instruction.added_api_endpoints.append(url)
            else:
                instruction.added_web_pages.append(url)

        for match in KEYWORD_BLOCK_RE.findall(normalized_text):
            instruction.positive_keywords.extend(self._split_keywords(match))
            instruction.summary_focus.extend(self._split_keywords(match))

        for match in SOURCE_BLOCK_RE.findall(normalized_text):
            parts = self._split_keywords(match)
            for part in parts:
                if part.startswith("http"):
                    if "rss" in part.lower() or part.lower().endswith(".xml"):
                        instruction.added_rss_feeds.append(part)
                    else:
                        instruction.added_web_pages.append(part)
                else:
                    instruction.preferred_sources.append(part)

        max_char_match = MAX_CHAR_RE.search(normalized_text)
        if max_char_match:
            instruction.summary_max_chars = int(max_char_match.group(1))

        summary_marker = SUMMARY_LENGTH_RE.search(normalized_text)
        if summary_marker:
            marker = summary_marker.group(1)
            if marker in {"太长", "精简", "简短"}:
                instruction.summary_style = "concise"
                instruction.summary_max_chars = instruction.summary_max_chars or 80
            elif marker in {"太短", "详细", "展开"}:
                instruction.summary_style = "detailed"
                instruction.summary_max_chars = instruction.summary_max_chars or 160

        candidate_keywords = self._extract_candidate_keywords(normalized_text)
        if POSITIVE_RE.search(normalized_text):
            instruction.satisfaction = "positive"
            instruction.positive_keywords.extend(candidate_keywords)
        if NEGATIVE_RE.search(normalized_text):
            instruction.satisfaction = "negative"
            instruction.negative_keywords.extend(candidate_keywords)

        instruction.article_feedbacks.extend(self._extract_article_feedbacks(normalized_text))
        instruction.added_rss_feeds = self._dedupe_list(instruction.added_rss_feeds)
        instruction.added_web_pages = self._dedupe_list(instruction.added_web_pages)
        instruction.added_api_endpoints = self._dedupe_list(instruction.added_api_endpoints)
        instruction.positive_keywords = self._dedupe_list(instruction.positive_keywords)
        instruction.negative_keywords = self._dedupe_list(instruction.negative_keywords)
        instruction.preferred_sources = self._dedupe_list(instruction.preferred_sources)
        instruction.summary_focus = self._dedupe_list(instruction.summary_focus)
        return instruction

    @staticmethod
    def _clean_reply_text(text: str) -> str:
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(">"):
                continue
            if stripped.lower().startswith(("from:", "sent:", "subject:", "to:")):
                continue
            lines.append(stripped)
        return "\n".join(lines[:300]).strip()

    @staticmethod
    def _split_keywords(text: str) -> List[str]:
        return [token.strip() for token in re.split(r"[,，;；/\n]", text) if token.strip()]

    @staticmethod
    def _extract_candidate_keywords(text: str) -> List[str]:
        candidates = []
        for phrase in [
            "single-cell sequencing",
            "spatial transcriptomics",
            "multiomics",
            "long-read sequencing",
            "liquid biopsy",
            "precision medicine",
            "临床测序",
            "肿瘤测序",
            "单细胞",
            "空间转录组",
        ]:
            if phrase.lower() in text.lower():
                candidates.append(phrase)
        return candidates

    @staticmethod
    def _dedupe_list(items: List[str]) -> List[str]:
        seen = set()
        result = []
        for item in items:
            normalized = item.strip()
            lowered = normalized.lower()
            if normalized and lowered not in seen:
                seen.add(lowered)
                result.append(normalized)
        return result

    def _extract_article_feedbacks(self, text: str) -> List[ArticleFeedbackAction]:
        actions: List[ArticleFeedbackAction] = []
        urls = URL_RE.findall(text)
        ids = NEWS_ID_RE.findall(text)
        title_matches = TITLE_FEEDBACK_RE.findall(text)

        is_like = any(p.search(text) for p in LIKE_PATTERNS)
        is_dislike = any(p.search(text) for p in DISLIKE_PATTERNS)
        vote = 1 if is_like and not is_dislike else (-1 if is_dislike else 0)
        if vote == 0:
            return actions

        for news_id in ids:
            actions.append(
                ArticleFeedbackAction(
                    article_id=news_id.lower(),
                    vote=vote,
                    reason="explicit_article_vote",
                )
            )

        for url in urls:
            actions.append(
                ArticleFeedbackAction(
                    article_link=url,
                    vote=vote,
                    reason="explicit_article_vote",
                )
            )

        for title in title_matches:
            clean = title.strip().strip('"“”')
            if clean:
                actions.append(
                    ArticleFeedbackAction(
                        article_title=clean,
                        vote=vote,
                        reason="explicit_article_vote_title",
                    )
                )

        if not ids and not urls and not title_matches:
            for line in text.splitlines():
                if any(p.search(line) for p in LIKE_PATTERNS + DISLIKE_PATTERNS):
                    candidate = re.sub(
                        r"(?:点赞|点踩|like|dislike|thumbs?\s*up|thumbs?\s*down)\s*[:：-]?",
                        "",
                        line,
                        flags=re.IGNORECASE,
                    ).strip()
                    if candidate and len(candidate) > 8:
                        actions.append(
                            ArticleFeedbackAction(
                                article_title=candidate,
                                vote=vote,
                                reason="explicit_article_vote_title_fuzzy",
                            )
                        )

        deduped = []
        seen = set()
        for action in actions:
            key = (action.article_id or "", action.article_link or "", action.article_title or "", action.vote)
            if key not in seen:
                seen.add(key)
                deduped.append(action)
        return deduped


class LLMFeedbackParser(BaseFeedbackParser):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.session = requests.Session()
        if settings.proxies:
            self.session.proxies.update(settings.proxies)
        self.session.headers.update(
            {
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            }
        )
        self.endpoint = f"{settings.llm_base_url}/chat/completions"
        self.rule_parser = RuleFeedbackParser(settings)

    def parse(self, sender: str, subject: str, text: str, message_id: str = "") -> FeedbackInstruction:
        normalized_text = self.rule_parser._clean_reply_text(text)
        fallback = self.rule_parser.parse(sender, subject, normalized_text, message_id)

        prompt = (
            "你是邮件反馈解析器。请把下面的用户反馈解析成 JSON。"
            "只输出 JSON，不要加解释。字段必须包含："
            "added_rss_feeds, added_web_pages, added_api_endpoints, "
            "positive_keywords, negative_keywords, preferred_sources, disliked_sources, "
            "summary_style, summary_max_chars, summary_focus, satisfaction, article_feedbacks。"
            "article_feedbacks 是数组，每项包含 article_id, article_link, article_title, vote(-1/1), reason。"
            "无法确定则返回空数组或 null。\n\n"
            f"邮件主题：{subject}\n"
            f"邮件正文：{normalized_text}"
        )

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": "你只返回合法 JSON，不要输出 Markdown。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": min(self.settings.llm_max_tokens, 700),
        }

        last_exc = None
        for attempt in range(1, self.settings.llm_max_retries + 1):
            try:
                response = self.session.post(self.endpoint, json=payload, timeout=self.settings.llm_timeout)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(f"transient status={response.status_code}: {response.text[:300]}")
                response.raise_for_status()
                content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                data = self._parse_json(content)
                if not isinstance(data, dict):
                    raise ValueError(f"LLM 返回不是 JSON 对象: {content[:300]}")

                return FeedbackInstruction(
                    sender=sender,
                    subject=subject,
                    received_at=datetime.now(timezone.utc).isoformat(),
                    raw_text=normalized_text,
                    message_id=message_id,
                    added_rss_feeds=self._merge_list(fallback.added_rss_feeds, data.get("added_rss_feeds", [])),
                    added_web_pages=self._merge_list(fallback.added_web_pages, data.get("added_web_pages", [])),
                    added_api_endpoints=self._merge_list(fallback.added_api_endpoints, data.get("added_api_endpoints", [])),
                    positive_keywords=self._merge_list(fallback.positive_keywords, data.get("positive_keywords", [])),
                    negative_keywords=self._merge_list(fallback.negative_keywords, data.get("negative_keywords", [])),
                    preferred_sources=self._merge_list(fallback.preferred_sources, data.get("preferred_sources", [])),
                    disliked_sources=self._merge_list(fallback.disliked_sources, data.get("disliked_sources", [])),
                    summary_style=data.get("summary_style") or fallback.summary_style,
                    summary_max_chars=data.get("summary_max_chars") or fallback.summary_max_chars,
                    summary_focus=self._merge_list(fallback.summary_focus, data.get("summary_focus", [])),
                    satisfaction=data.get("satisfaction") or fallback.satisfaction,
                    article_feedbacks=self._merge_article_feedback(
                        fallback.article_feedbacks,
                        data.get("article_feedbacks", []),
                    ),
                    parsed_by="llm+rule",
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("LLM 反馈解析失败，第 %s/%s 次重试: %s", attempt, self.settings.llm_max_retries, exc)
                if attempt < self.settings.llm_max_retries:
                    time.sleep(min(2**attempt, 8))

        logger.warning("LLM 反馈解析失败，回退规则解析: %s", last_exc)
        return fallback

    @staticmethod
    def _parse_json(content: str):
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?", "", content).strip()
            content = re.sub(r"```$", "", content).strip()
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            content = content[start : end + 1]
        return json.loads(content)

    @staticmethod
    def _merge_list(a, b):
        items = []
        seen = set()
        for part in list(a or []) + list(b or []):
            if not isinstance(part, str):
                continue
            normalized = part.strip()
            lowered = normalized.lower()
            if normalized and lowered not in seen:
                seen.add(lowered)
                items.append(normalized)
        return items

    @staticmethod
    def _merge_article_feedback(existing: List[ArticleFeedbackAction], raw_items) -> List[ArticleFeedbackAction]:
        result = list(existing)
        seen = {(item.article_id or "", item.article_link or "", item.article_title or "", item.vote) for item in existing}
        for item in raw_items or []:
            if not isinstance(item, dict):
                continue
            action = ArticleFeedbackAction(
                article_id=str(item.get("article_id") or "").strip() or None,
                article_link=str(item.get("article_link") or "").strip() or None,
                article_title=str(item.get("article_title") or "").strip() or None,
                vote=int(item.get("vote") or 0),
                reason=str(item.get("reason") or "llm_parse"),
            )
            key = (action.article_id or "", action.article_link or "", action.article_title or "", action.vote)
            if action.vote in {-1, 1} and key not in seen:
                seen.add(key)
                result.append(action)
        return result


class FeedbackProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profile_store = UserProfileStore(settings)
        self.logger = FeedbackLogger(settings.feedback_log_path)
        self.parser = (
            LLMFeedbackParser(settings)
            if settings.feedback_use_llm_parser or settings.feedback_parser_provider.lower() == "llm"
            else RuleFeedbackParser(settings)
        )
        self.state_store = NewsStateStore(settings.state_db_path)
        self.mailer = Mailer(settings)

    def process_feedbacks(self) -> List[FeedbackInstruction]:
        instructions: List[FeedbackInstruction] = []
        if not self.settings.feedback_enabled:
            return instructions

        for instruction in self._collect_feedback_from_imap():
            profile = self.profile_store.load(instruction.sender)
            profile = self.profile_store.apply_decay(profile)
            profile = self._apply_instruction(profile, instruction)
            self.profile_store.save(profile)

            self.profile_store.log_feedback_event(
                instruction.sender,
                instruction.message_id,
                instruction.received_at,
                instruction.subject,
                instruction.parsed_by,
                instruction.raw_text,
                json.dumps(asdict(instruction), ensure_ascii=False),
            )
            self.logger.append(instruction)

            if instruction.message_id:
                self.profile_store.mark_message_processed(instruction.message_id)

            if self.settings.feedback_auto_reply:
                try:
                    self.mailer.send_feedback_ack(instruction.sender, instruction)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("发送反馈回执失败: %s", exc)

            instructions.append(instruction)

        return instructions

    def _collect_feedback_from_imap(self) -> List[FeedbackInstruction]:
        mailbox = self._connect_imap()
        if mailbox is None:
            return []

        instructions: List[FeedbackInstruction] = []
        selected = False

        try:
            mailbox_name = (self.settings.feedback_mailbox or "INBOX").strip() or "INBOX"
            logger.info("准备选择反馈邮箱文件夹: %s", mailbox_name)

            status, select_data = mailbox.select(mailbox_name)
            logger.info("IMAP select 返回: status=%s data=%s", status, select_data)

            if status != "OK":
                logger.warning("选择反馈邮箱文件夹失败: mailbox=%s data=%s", mailbox_name, select_data)
                return []

            selected = True

            criteria = (self.settings.feedback_search_criteria or "UNSEEN").strip() or "UNSEEN"
            logger.info("开始搜索反馈邮件: criteria=%s", criteria)

            status, data = mailbox.search(None, criteria)
            logger.info("IMAP search 返回: status=%s data=%s", status, data)

            if status != "OK":
                logger.warning("搜索反馈邮件失败: %s", data)
                return []

            raw_ids = data[0] if data and len(data) > 0 else b""
            message_ids = raw_ids.split()[-self.settings.feedback_max_emails_per_run :]
            logger.info("本轮检测到 %s 封候选反馈邮件", len(message_ids))

            for msg_seq in message_ids:
                try:
                    status, msg_data = mailbox.fetch(msg_seq, "(RFC822)")
                    if status != "OK" or not msg_data:
                        logger.warning("获取邮件失败: seq=%s status=%s", msg_seq, status)
                        continue

                    raw_email = None
                    for item in msg_data:
                        if isinstance(item, tuple) and len(item) >= 2:
                            raw_email = item[1]
                            break

                    if not raw_email:
                        logger.warning("邮件内容为空: seq=%s", msg_seq)
                        continue

                    msg = email.message_from_bytes(raw_email)
                    sender = parseaddr(self._decode_header_value(msg.get("From", "")))[1].lower().strip()
                    subject = self._decode_header_value(msg.get("Subject", ""))
                    message_id = (msg.get("Message-ID", "") or "").strip()

                    if message_id and self.profile_store.is_message_processed(message_id):
                        logger.info("跳过已处理反馈邮件: %s", message_id)
                        if self.settings.feedback_mark_seen:
                            mailbox.store(msg_seq, "+FLAGS", "\\Seen")
                        continue

                    text = self._extract_text(msg)
                    if not self._is_feedback_email(subject, text):
                        logger.info("跳过非反馈邮件: subject=%s", subject)
                        continue

                    instruction = self.parser.parse(
                        sender=sender,
                        subject=subject,
                        text=text,
                        message_id=message_id,
                    )
                    instructions.append(instruction)

                    if self.settings.feedback_mark_seen:
                        mailbox.store(msg_seq, "+FLAGS", "\\Seen")

                except Exception as exc:  # noqa: BLE001
                    logger.warning("处理单封反馈邮件失败: seq=%s error=%s", msg_seq, exc)
                    continue

        finally:
            self._cleanup_mailbox(mailbox, selected=selected)

        return instructions

    def _connect_imap(self):
        try:
            host = self.settings.feedback_imap_host
            port = self.settings.feedback_imap_port
            use_ssl = self.settings.feedback_imap_use_ssl

            logger.info("连接反馈邮箱: host=%s port=%s ssl=%s", host, port, use_ssl)

            if use_ssl:
                mailbox = imaplib.IMAP4_SSL(host, port)
            else:
                mailbox = imaplib.IMAP4(host, port)

            login_status, login_data = mailbox.login(
                self.settings.feedback_email,
                self.settings.feedback_password,
            )
            logger.info("IMAP login 返回: status=%s data=%s", login_status, login_data)

            if login_status != "OK":
                raise RuntimeError(f"IMAP 登录失败: {login_data}")

            self._send_imap_id_if_needed(mailbox)
            return mailbox

        except Exception as exc:  # noqa: BLE001
            logger.warning("连接反馈邮箱失败: %s", exc)
            return None

    def _send_imap_id_if_needed(self, mailbox) -> None:
        """
        兼容网易系邮箱（163/126/yeah）可能要求的 IMAP ID。
        不是所有邮箱都需要，也不是所有服务端都支持，所以失败时只记日志，不中断主流程。
        """
        email_addr = (self.settings.feedback_email or "").lower().strip()
        if not any(domain in email_addr for domain in ("@163.com", "@126.com", "@yeah.net")):
            return

        try:
            if "ID" not in imaplib.Commands:
                imaplib.Commands["ID"] = ("AUTH",)

            client_name = "NGSNewsDigest"
            contact = self.settings.feedback_email or ""
            vendor = "OpenAI-Generated-Client"
            version = "1.0.0"

            # 按 RFC 2971 的 ID 参数格式构造
            id_args = f'("name" "{client_name}" "contact" "{contact}" "vendor" "{vendor}" "version" "{version}")'
            typ, dat = mailbox._simple_command("ID", id_args)
            mailbox._untagged_response(typ, dat, "ID")
            logger.info("已发送网易兼容 IMAP ID 命令: status=%s data=%s", typ, dat)

        except Exception as exc:  # noqa: BLE001
            logger.warning("发送 IMAP ID 命令失败，继续后续流程: %s", exc)

    def _cleanup_mailbox(self, mailbox, selected: bool = False) -> None:
        if mailbox is None:
            return

        if selected:
            try:
                mailbox.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭已选中邮箱文件夹失败: %s", exc)

        try:
            mailbox.logout()
        except Exception as exc:  # noqa: BLE001
            logger.debug("IMAP logout 失败: %s", exc)

    def _is_feedback_email(self, subject: str, text: str) -> bool:
        haystack = f"{subject}\n{text[:800]}".lower()
        return any(token.lower() in haystack for token in self.settings.feedback_subject_keywords) or any(
            marker in haystack
            for marker in [
                "增加来源",
                "关键词",
                "关注",
                "摘要",
                "不相关",
                "feedback",
                "点赞",
                "点踩",
                "like",
                "dislike",
                "标题",
            ]
        )

    def _apply_instruction(self, profile: UserProfile, instruction: FeedbackInstruction) -> UserProfile:
        profile.custom_rss_feeds = self.profile_store.merge_unique(profile.custom_rss_feeds, instruction.added_rss_feeds)
        profile.custom_web_pages = self.profile_store.merge_unique(profile.custom_web_pages, instruction.added_web_pages)
        profile.custom_api_endpoints = self.profile_store.merge_unique(profile.custom_api_endpoints, instruction.added_api_endpoints)
        profile.summary_focus = self.profile_store.merge_unique(profile.summary_focus, instruction.summary_focus)

        if instruction.summary_style:
            profile.summary_style = instruction.summary_style

        if instruction.summary_max_chars:
            profile.summary_max_chars = max(40, min(300, int(instruction.summary_max_chars)))

        for keyword in instruction.positive_keywords:
            profile.preferred_keywords[keyword] = round(profile.preferred_keywords.get(keyword, 0.0) + 1.0, 4)

        for keyword in instruction.negative_keywords:
            profile.negative_keywords[keyword] = round(profile.negative_keywords.get(keyword, 0.0) + 1.0, 4)

        for source in instruction.preferred_sources:
            profile.preferred_sources[source] = round(profile.preferred_sources.get(source, 0.0) + 1.0, 4)

        for source in instruction.disliked_sources:
            profile.preferred_sources[source] = round(profile.preferred_sources.get(source, 0.0) - 1.0, 4)

        for action in instruction.article_feedbacks:
            fingerprint = self._resolve_fingerprint(profile.email, action)
            if not fingerprint or action.vote not in {-1, 1}:
                continue

            self.profile_store.record_article_feedback(
                profile.email,
                fingerprint,
                action.vote,
                article_title=action.article_title or "",
                article_link=action.article_link or "",
                reason=action.reason,
            )

        profile.explicit_article_feedback = self.profile_store.load_article_feedback(profile.email)
        profile.feedback_history_count += 1
        return profile

    @staticmethod
    def _decode_header_value(value: str) -> str:
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    def _extract_text(self, msg: Message) -> str:
        if msg.is_multipart():
            parts = []
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))
                if "attachment" in disposition.lower():
                    continue
                if content_type == "text/plain":
                    parts.append(self._decode_payload(part))
                elif content_type == "text/html" and not parts:
                    html = self._decode_payload(part)
                    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
                    html = re.sub(r"<[^>]+>", " ", html)
                    parts.append(html)
            return "\n".join(p for p in parts if p).strip()

        return self._decode_payload(msg).strip()

    @staticmethod
    def _decode_payload(part: Message) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except Exception:
            return payload.decode("utf-8", errors="replace")

    def _resolve_fingerprint(self, recipient: str, action: ArticleFeedbackAction) -> Optional[str]:
        if action.article_fingerprint:
            return action.article_fingerprint.lower()

        if action.article_id and re.fullmatch(r"[a-f0-9]{6,16}", action.article_id, re.IGNORECASE):
            return action.article_id.lower()

        if action.article_link:
            return f"url:{action.article_link.strip().lower()}"

        if action.article_title:
            match = self.state_store.find_recent_match_by_title(
                recipient,
                action.article_title,
                threshold=self.settings.fuzzy_title_match_threshold,
            )
            if match:
                logger.info(
                    "标题模糊匹配成功: %s -> %s (score=%s)",
                    action.article_title,
                    match.get("title"),
                    match.get("score"),
                )
                return str(match["fingerprint"])

        return None
