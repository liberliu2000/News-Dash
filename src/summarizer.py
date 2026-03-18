from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import requests

from .config import Settings
from .fetcher import NewsItem
from .profile_manager import UserProfile

logger = logging.getLogger(__name__)


@dataclass
class SummarizedNews:
    item: NewsItem
    summary: str


class ModelClient:
    def __init__(self, settings: Settings, profile: Optional[UserProfile] = None):
        self.settings = settings
        self.profile = profile or UserProfile()

    def summarize_news(self, item: NewsItem) -> str:
        raise NotImplementedError


class OpenAIClient(ModelClient):
    def __init__(self, settings: Settings, profile: Optional[UserProfile] = None):
        super().__init__(settings, profile)
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

    def summarize_news(self, item: NewsItem) -> str:
        summary_style = self.profile.summary_style or self.settings.summary_style
        max_chars = self.profile.summary_max_chars or self.settings.summary_max_chars
        focus_topics = self._build_focus_topics()
        prompt = (
            "你是一个 NGS 领域的专家，请根据用户偏好对新闻做中文摘要。"
            f"摘要风格：{summary_style}。"
            f"目标长度：不超过 {max_chars} 字。"
            "优先突出技术进展、产品动态、产业竞争、临床转化和科研意义。"
            f"若用户关注主题非空，请优先围绕这些主题组织表达：{focus_topics or '无特殊侧重'}。"
            "最后单独附上一行“原文链接：<url>”。\n\n"
            f"新闻标题：{item.title}\n"
            f"新闻来源：{item.source}\n"
            f"发布时间：{item.published_at.isoformat() if item.published_at else '未知'}\n"
            f"新闻内容：{item.content or item.summary}\n"
            f"原文链接：{item.link}"
        )

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": "你是一名熟悉 NGS、基因组学、单细胞与多组学产业的资深分析师。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": self.settings.llm_temperature,
            "max_tokens": self._effective_max_tokens(),
        }

        last_error: Optional[Exception] = None
        for attempt in range(1, self.settings.llm_max_retries + 1):
            try:
                response = self.session.post(self.endpoint, json=payload, timeout=self.settings.llm_timeout)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(f"transient status={response.status_code}, body={response.text[:500]}")
                response.raise_for_status()
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if not content:
                    raise ValueError(f"模型返回为空: {data}")
                return content
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("LLM 摘要失败，第 %s/%s 次重试: %s", attempt, self.settings.llm_max_retries, exc)
                if attempt < self.settings.llm_max_retries:
                    time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"摘要生成失败: {last_error}")

    def _build_focus_topics(self) -> str:
        topics = []
        for topic in list(self.settings.summary_focus) + list(self.profile.summary_focus):
            if topic not in topics:
                topics.append(topic)
        return ", ".join(topics)

    def _effective_max_tokens(self) -> int:
        max_chars = self.profile.summary_max_chars or self.settings.summary_max_chars
        if max_chars <= 80:
            return min(self.settings.llm_max_tokens, 250)
        if max_chars <= 120:
            return min(self.settings.llm_max_tokens, 350)
        return self.settings.llm_max_tokens


class DoubaoClient(ModelClient):
    def summarize_news(self, item: NewsItem) -> str:
        raise NotImplementedError("当前项目已预留 DoubaoClient 结构，待接入具体 API 后实现。")


class NewsSummarizer:
    def __init__(self, settings: Settings, profile: Optional[UserProfile] = None):
        self.settings = settings
        self.profile = profile or UserProfile()
        self.client = self._build_client()

    def _build_client(self) -> ModelClient:
        provider = self.settings.model_provider.lower()
        if provider == "openai_compatible":
            return OpenAIClient(self.settings, self.profile)
        if provider == "doubao":
            return DoubaoClient(self.settings, self.profile)
        raise ValueError(f"不支持的模型提供商: {self.settings.model_provider}")

    def summarize(self, items: List[NewsItem]) -> List[SummarizedNews]:
        results: List[SummarizedNews] = []
        for index, item in enumerate(items, start=1):
            logger.info("开始摘要第 %s/%s 条新闻: %s", index, len(items), item.title)
            summary = self.client.summarize_news(item)
            results.append(SummarizedNews(item=item, summary=summary))
        return results
