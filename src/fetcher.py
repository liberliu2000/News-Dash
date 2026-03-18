from __future__ import annotations

import hashlib
import logging
import random
import re
import sqlite3
import time
from difflib import SequenceMatcher
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import feedparser
import requests
from bs4 import BeautifulSoup

from .config import Settings
from .profile_manager import UserProfile

logger = logging.getLogger(__name__)

COMMON_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]


@dataclass
class NewsItem:
    source: str
    title: str
    link: str
    published_at: Optional[datetime]
    summary: str
    content: str
    fingerprint: str = ""
    news_id: str = ""
    relevance_score: float = 0.0


class NewsStateStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pushed_news (
                    recipient TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    source TEXT,
                    title TEXT,
                    link TEXT,
                    published_at TEXT,
                    pushed_at TEXT NOT NULL,
                    PRIMARY KEY (recipient, fingerprint)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pushed_at ON pushed_news(pushed_at)")
            conn.commit()

    def has_seen(self, recipient: str, fingerprint: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM pushed_news WHERE recipient = ? AND fingerprint = ? LIMIT 1", (recipient.strip().lower(), fingerprint)).fetchone()
        return row is not None

    def mark_pushed(self, recipient: str, items: Iterable[NewsItem]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = [
            (recipient.strip().lower(), item.fingerprint, item.source, item.title, item.link, item.published_at.isoformat() if item.published_at else None, now_iso)
            for item in items if item.fingerprint
        ]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO pushed_news (recipient, fingerprint, source, title, link, published_at, pushed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

    def find_recent_match_by_title(self, recipient: str, title: str, days: int = 30, threshold: float = 0.62) -> Optional[Dict[str, str]]:
        candidate = (title or "").strip()
        if not candidate:
            return None
        threshold_iso = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT fingerprint, title, link, source, pushed_at FROM pushed_news WHERE recipient = ? AND pushed_at >= ? ORDER BY pushed_at DESC LIMIT 200",
                (recipient.strip().lower(), threshold_iso),
            ).fetchall()
        best = None
        best_score = 0.0
        target = self._normalize_title(candidate)
        for row in rows:
            score = self._title_similarity(target, self._normalize_title(row["title"] or ""))
            if score > best_score:
                best_score = score
                best = row
        if best is None or best_score < threshold:
            return None
        return {"fingerprint": best["fingerprint"], "title": best["title"], "link": best["link"], "source": best["source"], "score": round(best_score,4)}

    @staticmethod
    def _normalize_title(title: str) -> str:
        title = re.sub(r"\s+", " ", (title or "").strip().lower())
        return re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", " ", title).strip()

    @staticmethod
    def _title_similarity(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        ratio = SequenceMatcher(None, a, b).ratio()
        a_tokens = set(a.split())
        b_tokens = set(b.split())
        overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
        return max(ratio, overlap * 1.15)

    def cleanup(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        threshold = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM pushed_news WHERE pushed_at < ?", (threshold,))
            conn.commit()
            return cursor.rowcount or 0


class NewsFetcher:
    def __init__(self, settings: Settings, profile: Optional[UserProfile] = None):
        self.settings = settings
        self.profile = profile or UserProfile()
        self.session = requests.Session()
        self.last_request_at_by_domain: Dict[str, float] = {}
        self.robots_cache: Dict[str, RobotFileParser] = {}
        self._refresh_session_headers()
        if settings.proxies:
            self.session.proxies.update(settings.proxies)
        self.state_store = NewsStateStore(settings.state_db_path)

    def fetch_candidates(self) -> List[NewsItem]:
        items: List[NewsItem] = []
        all_feeds = self._merge_lists(self.settings.rss_feeds, self.profile.custom_rss_feeds)
        all_pages = self._merge_lists(self.settings.web_pages, self.profile.custom_web_pages)
        all_api_endpoints = self._merge_lists(self.settings.news_api_endpoints, self.profile.custom_api_endpoints)
        for feed_url in all_feeds:
            try:
                items.extend(self._fetch_feed(feed_url))
            except Exception as exc:  # noqa: BLE001
                logger.exception("抓取 RSS 失败: %s, error=%s", feed_url, exc)
        for page_url in all_pages:
            try:
                items.extend(self._fetch_web_page(page_url))
            except Exception as exc:  # noqa: BLE001
                logger.exception("抓取网页失败: %s, error=%s", page_url, exc)
        for api_endpoint in all_api_endpoints:
            try:
                items.extend(self._fetch_api(api_endpoint))
            except Exception as exc:  # noqa: BLE001
                logger.exception("抓取 API 失败: %s, error=%s", api_endpoint, exc)
        deduped = self._deduplicate(items)
        filtered = self._filter_items(deduped)
        deleted = self.state_store.cleanup(self.settings.state_retention_days)
        if deleted:
            logger.info("已清理 %s 条过期去重记录", deleted)
        return filtered

    def personalize_for_user(self, items: List[NewsItem], profile: UserProfile, recipient: str) -> List[NewsItem]:
        results = [self._clone_item(item) for item in items if not self.state_store.has_seen(recipient, item.fingerprint)]
        for item in results:
            item.relevance_score = self._score_item(item, profile)
        results.sort(key=lambda x: (x.relevance_score, x.published_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return results[: self.settings.max_news_items]

    def mark_as_sent(self, recipient: str, items: Iterable[NewsItem]) -> None:
        self.state_store.mark_pushed(recipient, items)

    @staticmethod
    def _clone_item(item: NewsItem) -> NewsItem:
        return NewsItem(source=item.source, title=item.title, link=item.link, published_at=item.published_at, summary=item.summary, content=item.content, fingerprint=item.fingerprint, news_id=item.news_id, relevance_score=item.relevance_score)

    def _refresh_session_headers(self, referer: Optional[str] = None) -> None:
        headers = {
            "User-Agent": random.choice(COMMON_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": random.choice(self.settings.accept_languages),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "DNT": "1",
        }
        if referer:
            headers["Referer"] = referer
        self.session.headers.clear()
        self.session.headers.update(headers)

    def _request_with_pacing(self, url: str, *, timeout: Optional[int] = None, allow_redirects: bool = True, referer: Optional[str] = None) -> requests.Response:
        parsed = urlparse(url)
        domain_key = f"{parsed.scheme}://{parsed.netloc}"
        if self.settings.respect_robots_txt and not self._is_allowed_by_robots(url):
            raise RuntimeError(f"robots.txt 不允许抓取: {url}")
        self._wait_for_domain_slot(domain_key)
        referer = referer if (self.settings.enable_referer and referer) else (domain_key + "/" if self.settings.enable_referer else None)
        last_exc = None
        timeout = timeout or self.settings.http_timeout
        for attempt in range(1, self.settings.max_request_retries + 1):
            self._refresh_session_headers(referer=referer)
            try:
                response = self.session.get(url, timeout=timeout, allow_redirects=allow_redirects)
                self.last_request_at_by_domain[domain_key] = time.time()
                if response.status_code in {429, 500, 502, 503, 504}:
                    wait_seconds = self._compute_backoff(attempt, response)
                    logger.warning("请求返回 %s，放慢节奏后重试: %s (attempt=%s/%s, sleep=%.2fs)", response.status_code, url, attempt, self.settings.max_request_retries, wait_seconds)
                    time.sleep(wait_seconds)
                    continue
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_exc = exc
                self.last_request_at_by_domain[domain_key] = time.time()
                if attempt >= self.settings.max_request_retries:
                    break
                wait_seconds = self._compute_backoff(attempt)
                logger.warning("请求异常，稍后重试: %s (attempt=%s/%s, sleep=%.2fs, error=%s)", url, attempt, self.settings.max_request_retries, wait_seconds, exc)
                time.sleep(wait_seconds)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"请求失败: {url}")

    def _compute_backoff(self, attempt: int, response: Optional[requests.Response] = None) -> float:
        retry_after = None
        if response is not None and response.headers.get("Retry-After"):
            try:
                retry_after = float(response.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = None
        if retry_after is not None:
            return min(retry_after, self.settings.max_retry_backoff_seconds)
        jitter = random.uniform(0.2, self.settings.request_jitter_seconds)
        base = self.settings.base_retry_backoff_seconds * (2 ** max(0, attempt - 1))
        return min(base + jitter, self.settings.max_retry_backoff_seconds)

    def _wait_for_domain_slot(self, domain_key: str) -> None:
        last_ts = self.last_request_at_by_domain.get(domain_key)
        required_gap = random.uniform(self.settings.min_request_interval_seconds, self.settings.max_request_interval_seconds)
        if last_ts is None:
            if required_gap > 0:
                time.sleep(required_gap)
            return
        elapsed = time.time() - last_ts
        remaining = required_gap - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _is_allowed_by_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        domain_key = f"{parsed.scheme}://{parsed.netloc}"
        parser = self.robots_cache.get(domain_key)
        if parser is None:
            robots_url = urljoin(domain_key, "/robots.txt")
            parser = RobotFileParser()
            parser.set_url(robots_url)
            try:
                response = self.session.get(robots_url, timeout=min(self.settings.http_timeout, 10))
                if response.ok and response.text:
                    parser.parse(response.text.splitlines())
                else:
                    parser = RobotFileParser()
            except Exception:
                parser = RobotFileParser()
            self.robots_cache[domain_key] = parser
        user_agent = self.session.headers.get("User-Agent", "*")
        try:
            return parser.can_fetch(user_agent, url)
        except Exception:
            return True

    def _fetch_feed(self, feed_url: str) -> List[NewsItem]:
        logger.info("读取 RSS: %s", feed_url)
        response = self._request_with_pacing(feed_url)
        parsed = feedparser.parse(response.content)
        feed_title = getattr(parsed.feed, "title", feed_url)
        results: List[NewsItem] = []
        for entry in parsed.entries:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            if not title or not link:
                continue
            summary = self._html_to_text(getattr(entry, "summary", "") or getattr(entry, "description", ""))
            content = summary
            if self.settings.fetch_full_content and link:
                full_text = self._fetch_article_text(link, referer=feed_url)
                if full_text:
                    content = full_text
            published_at = self._parse_date(entry)
            item = NewsItem(str(feed_title), title, link, published_at, summary, content)
            item.fingerprint = self._build_fingerprint(item)
            item.news_id = item.fingerprint[:12]
            results.append(item)
        return results

    def _fetch_web_page(self, page_url: str) -> List[NewsItem]:
        logger.info("读取网页列表页: %s", page_url)
        response = self._request_with_pacing(page_url)
        soup = BeautifulSoup(response.text, "lxml")
        link_candidates: List[str] = []
        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            text = anchor.get_text(" ", strip=True)
            if not href or not text:
                continue
            absolute_href = urljoin(page_url, href)
            if absolute_href.startswith("http") and self._looks_like_article_link(absolute_href, text):
                link_candidates.append(absolute_href)
        results: List[NewsItem] = []
        for link in self._deduplicate_links(link_candidates)[: self.settings.max_links_per_page]:
            article = self._fetch_article_metadata(link, source=page_url)
            if article:
                results.append(article)
        return results

    def _fetch_api(self, endpoint_template: str) -> List[NewsItem]:
        today = datetime.now(timezone.utc)
        from_date = (today - timedelta(hours=self.settings.lookback_hours)).strftime("%Y-%m-%d")
        all_results: List[NewsItem] = []
        queries = self.settings.api_queries or [""]
        for query in queries:
            url = endpoint_template.format(query=requests.utils.quote(query), api_key=self.settings.news_api_key, from_date=from_date)
            response = self._request_with_pacing(url)
            all_results.extend(self._parse_api_payload(response.json(), source=url))
        return all_results

    def _parse_api_payload(self, payload: Dict, source: str) -> List[NewsItem]:
        records = []
        candidates = []
        if isinstance(payload, dict):
            for key in ("articles", "data", "results", "news"):
                value = payload.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
        for item_data in candidates:
            if not isinstance(item_data, dict):
                continue
            title = str(item_data.get("title") or item_data.get("name") or "").strip()
            link = str(item_data.get("url") or item_data.get("link") or item_data.get("source_url") or "").strip()
            summary = str(item_data.get("description") or item_data.get("summary") or "").strip()
            content = str(item_data.get("content") or summary).strip()
            published_at = self._parse_datetime_str(item_data.get("publishedAt") or item_data.get("published_at") or item_data.get("pubDate") or item_data.get("created_at"))
            if not title or not link:
                continue
            article = NewsItem(source=self._extract_domain(source), title=title, link=link, published_at=published_at, summary=summary, content=self._shrink_text(content, max_chars=5000))
            article.fingerprint = self._build_fingerprint(article)
            article.news_id = article.fingerprint[:12]
            records.append(article)
        return records

    def _fetch_article_text(self, url: str, referer: Optional[str] = None) -> str:
        try:
            response = self._request_with_pacing(url, referer=referer)
            soup = BeautifulSoup(response.text, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            article = soup.find("article")
            if article:
                text = article.get_text(" ", strip=True)
            else:
                paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
                text = " ".join(paragraphs)
            return self._shrink_text(text, max_chars=5000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("抓取正文失败: %s, error=%s", url, exc)
            return ""

    def _fetch_article_metadata(self, url: str, source: str) -> Optional[NewsItem]:
        try:
            response = self._request_with_pacing(url, referer=source)
            soup = BeautifulSoup(response.text, "lxml")
            title = ""
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content"):
                title = og_title["content"].strip() or title
            description = ""
            for attrs in [{"name": "description"}, {"property": "og:description"}]:
                meta = soup.find("meta", attrs=attrs)
                if meta and meta.get("content"):
                    description = meta["content"].strip()
                    break
            content = self._fetch_article_text(url, referer=source)
            published_at = self._extract_published_at_from_html(soup)
            if not title:
                return None
            item = NewsItem(source=self._extract_domain(source), title=self._shrink_text(title, max_chars=300), link=url, published_at=published_at, summary=self._shrink_text(description, max_chars=1000), content=content or description)
            item.fingerprint = self._build_fingerprint(item)
            item.news_id = item.fingerprint[:12]
            return item
        except Exception as exc:  # noqa: BLE001
            logger.warning("抓取文章元数据失败: %s, error=%s", url, exc)
            return None

    def _deduplicate(self, items: List[NewsItem]) -> List[NewsItem]:
        seen = set()
        results = []
        for item in items:
            if not item.fingerprint:
                item.fingerprint = self._build_fingerprint(item)
            item.news_id = item.fingerprint[:12]
            if item.fingerprint in seen:
                continue
            seen.add(item.fingerprint)
            results.append(item)
        return results

    def _filter_items(self, items: List[NewsItem]) -> List[NewsItem]:
        results = []
        threshold = datetime.now(timezone.utc) - timedelta(hours=self.settings.lookback_hours) if self.settings.lookback_hours > 0 else None
        effective_keywords = self._effective_keywords(self.profile)
        for item in items:
            if threshold and item.published_at and item.published_at.astimezone(timezone.utc) < threshold:
                continue
            blob = f"{item.title}\n{item.summary}\n{item.content}".lower()
            if effective_keywords and not any(keyword.lower() in blob for keyword in effective_keywords):
                continue
            results.append(item)
        return results

    def _score_item(self, item: NewsItem, profile: UserProfile) -> float:
        score = 0.0
        blob = f"{item.title}\n{item.summary}\n{item.content}".lower()
        source = item.source.lower()
        link = item.link.lower()
        for keyword, weight in profile.preferred_keywords.items():
            if keyword.lower() in blob:
                score += weight * self.settings.preferred_keywords_weight
        for keyword, weight in profile.negative_keywords.items():
            if keyword.lower() in blob:
                score += weight * self.settings.negative_keywords_weight
        for source_keyword, weight in profile.preferred_sources.items():
            marker = source_keyword.lower()
            if marker and (marker in source or marker in link):
                score += weight * self.settings.preferred_sources_weight
        explicit_score = self._resolve_explicit_feedback_score(profile, item)
        score += explicit_score * self.settings.explicit_article_feedback_weight
        if item.published_at:
            age_hours = max((datetime.now(timezone.utc) - item.published_at.astimezone(timezone.utc)).total_seconds() / 3600, 0)
            score += max(0.0, 24.0 - min(age_hours, 24.0)) / 24.0
        return round(score, 4)

    @staticmethod
    def _resolve_explicit_feedback_score(profile: UserProfile, item: NewsItem) -> float:
        if item.fingerprint in profile.explicit_article_feedback:
            return profile.explicit_article_feedback[item.fingerprint]
        link_key = f"url:{item.link.strip().lower()}"
        if link_key in profile.explicit_article_feedback:
            return profile.explicit_article_feedback[link_key]
        for key, value in profile.explicit_article_feedback.items():
            if key.startswith("url:"):
                continue
            if len(key) <= 16 and item.fingerprint.startswith(key):
                return value
        return 0.0

    def _effective_keywords(self, profile: UserProfile) -> List[str]:
        dynamic_keywords = list(profile.preferred_keywords.keys()) + list(self.settings.summary_focus) + list(profile.summary_focus)
        return self._merge_lists(self.settings.keywords, dynamic_keywords)

    @staticmethod
    def _merge_lists(*lists: List[str]) -> List[str]:
        seen = set()
        results = []
        for items in lists:
            for item in items:
                normalized = item.strip()
                key = normalized.lower()
                if normalized and key not in seen:
                    seen.add(key)
                    results.append(normalized)
        return results

    @staticmethod
    def _build_fingerprint(item: NewsItem) -> str:
        raw = f"{item.source}|{item.title}|{item.link}".lower().encode("utf-8", errors="ignore")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _extract_domain(url: str) -> str:
        match = re.match(r"^(https?://[^/]+)", url)
        return match.group(1) if match else url

    @staticmethod
    def _deduplicate_links(links: Iterable[str]) -> List[str]:
        seen = set()
        results = []
        for link in links:
            normalized = link.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                results.append(normalized)
        return results

    @staticmethod
    def _looks_like_article_link(href: str, text: str) -> bool:
        href_lower = href.lower()
        text_lower = text.lower()
        article_hints = ["/news/", "/article/", "/articles/", "/202", "/20", "biorxiv", "genomeweb", "sequencing", "genomics"]
        return any(hint in href_lower or hint in text_lower for hint in article_hints)

    @staticmethod
    def _html_to_text(html: str) -> str:
        if not html:
            return ""
        return BeautifulSoup(html, "lxml").get_text(" ", strip=True)

    @staticmethod
    def _shrink_text(text: str, max_chars: int = 5000) -> str:
        text = re.sub(r"\s+", " ", (text or "")).strip()
        return text[:max_chars]

    @staticmethod
    def _parse_datetime_str(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = parsedate_to_datetime(value)
        except Exception:
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _parse_date(self, entry) -> Optional[datetime]:
        for attr in ["published_parsed", "updated_parsed"]:
            value = getattr(entry, attr, None)
            if value:
                try:
                    return datetime(*value[:6], tzinfo=timezone.utc)
                except Exception:
                    pass
        for attr in ["published", "updated"]:
            value = getattr(entry, attr, None)
            parsed = self._parse_datetime_str(value)
            if parsed:
                return parsed
        return None

    def _extract_published_at_from_html(self, soup: BeautifulSoup) -> Optional[datetime]:
        candidates = []
        for attrs in [{"property": "article:published_time"}, {"name": "pubdate"}, {"name": "publish-date"}, {"name": "date"}]:
            meta = soup.find("meta", attrs=attrs)
            if meta and meta.get("content"):
                candidates.append(meta.get("content"))
        time_tag = soup.find("time")
        if time_tag:
            candidates.append(time_tag.get("datetime") or time_tag.get_text(" ", strip=True))
        for candidate in candidates:
            parsed = self._parse_datetime_str(candidate)
            if parsed:
                return parsed
        return None
