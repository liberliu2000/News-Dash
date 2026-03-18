from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()


DEFAULT_RSS_FEEDS = [
    "https://www.genomeweb.com/rss.xml",
    "https://www.news-medical.net/tag/feed/Next-Generation-Sequencing.aspx",
    "https://www.sciencedaily.com/rss/plants_animals/genetics.xml",
    "https://www.sciencedaily.com/rss/health_medicine/genetics.xml",
    "https://connect.biorxiv.org/biorxiv_xml.php?subject=genomics",
]


@dataclass
class Settings:
    max_news_items: int = 10
    lookback_hours: int = 72
    keywords: List[str] = field(default_factory=list)
    fetch_full_content: bool = True

    model_provider: str = "openai_compatible"
    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model: str = "deepseek-r1-250120"
    llm_temperature: float = 0.3
    llm_timeout: int = 90
    llm_max_tokens: int = 600
    llm_max_retries: int = 3

    http_proxy: Optional[str] = None
    https_proxy: Optional[str] = None

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "NGS Daily Digest"
    smtp_from_email: str = ""
    smtp_to: List[str] = field(default_factory=list)
    smtp_cc: List[str] = field(default_factory=list)
    smtp_bcc: List[str] = field(default_factory=list)
    email_subject_prefix: str = "[NGS Daily Digest]"

    send_empty_email: bool = False
    log_level: str = "INFO"
    http_timeout: int = 20

    rss_feeds: List[str] = field(default_factory=lambda: DEFAULT_RSS_FEEDS.copy())
    web_pages: List[str] = field(default_factory=list)
    news_api_endpoints: List[str] = field(default_factory=list)
    api_queries: List[str] = field(default_factory=list)
    news_api_key: str = ""

    state_db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "news_state.db")
    state_retention_days: int = 180
    max_links_per_page: int = 20

    min_request_interval_seconds: float = 1.2
    max_request_interval_seconds: float = 3.5
    request_jitter_seconds: float = 0.8
    max_request_retries: int = 3
    base_retry_backoff_seconds: float = 2.0
    max_retry_backoff_seconds: float = 20.0
    respect_robots_txt: bool = True
    enable_referer: bool = True
    accept_languages: List[str] = field(default_factory=lambda: ["en-US,en;q=0.9", "en-GB,en;q=0.8", "zh-CN,zh;q=0.9,en;q=0.7"])

    feedback_enabled: bool = False
    feedback_imap_host: str = ""
    feedback_imap_port: int = 993
    feedback_imap_use_ssl: bool = True
    feedback_email: str = ""
    feedback_password: str = ""
    feedback_mailbox: str = "INBOX"
    feedback_search_criteria: str = "UNSEEN"
    feedback_mark_seen: bool = True
    feedback_subject_keywords: List[str] = field(default_factory=lambda: ["feedback", "digest", "日报", "新闻"])
    feedback_max_emails_per_run: int = 20
    feedback_parser_provider: str = "rule"
    feedback_use_llm_parser: bool = False
    feedback_auto_reply: bool = True

    profile_db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "profiles.db")
    feedback_log_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "feedback_log.jsonl")
    runtime_state_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "runtime_state.json")
    profile_decay_factor: float = 0.98
    preferred_sources_weight: float = 2.0
    preferred_keywords_weight: float = 1.2
    negative_keywords_weight: float = -1.0
    explicit_article_feedback_weight: float = 4.0
    fuzzy_title_match_threshold: float = 0.62

    summary_style: str = "concise"
    summary_max_chars: int = 100
    summary_focus: List[str] = field(default_factory=list)

    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8501
    dashboard_debug: bool = False
    dashboard_secret_key: str = "ngs-dashboard-secret"

    @property
    def proxies(self) -> dict:
        proxies = {}
        if self.http_proxy:
            proxies["http"] = self.http_proxy
        if self.https_proxy:
            proxies["https"] = self.https_proxy
        return proxies


def _split_csv(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _split_pipe(value: str) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split("|") if item.strip()]


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    return Settings(
        max_news_items=int(os.getenv("MAX_NEWS_ITEMS", "10")),
        lookback_hours=int(os.getenv("LOOKBACK_HOURS", "72")),
        keywords=_split_csv(os.getenv("KEYWORDS", "")),
        fetch_full_content=_get_bool("FETCH_FULL_CONTENT", True),
        model_provider=os.getenv("MODEL_PROVIDER", "openai_compatible").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/"),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "deepseek-r1-250120").strip(),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.3")),
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "90")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "600")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "3")),
        http_proxy=os.getenv("HTTP_PROXY") or None,
        https_proxy=os.getenv("HTTPS_PROXY") or None,
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
        smtp_use_ssl=_get_bool("SMTP_USE_SSL", True),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        smtp_from_name=os.getenv("SMTP_FROM_NAME", "NGS Daily Digest").strip(),
        smtp_from_email=os.getenv("SMTP_FROM_EMAIL", "").strip(),
        smtp_to=_split_csv(os.getenv("SMTP_TO", "")),
        smtp_cc=_split_csv(os.getenv("SMTP_CC", "")),
        smtp_bcc=_split_csv(os.getenv("SMTP_BCC", "")),
        email_subject_prefix=os.getenv("EMAIL_SUBJECT_PREFIX", "[NGS Daily Digest]").strip(),
        send_empty_email=_get_bool("SEND_EMPTY_EMAIL", False),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        http_timeout=int(os.getenv("HTTP_TIMEOUT", "20")),
        rss_feeds=_split_csv(os.getenv("RSS_FEEDS", "")) or DEFAULT_RSS_FEEDS.copy(),
        web_pages=_split_csv(os.getenv("WEB_PAGES", "")),
        news_api_endpoints=_split_csv(os.getenv("NEWS_API_ENDPOINTS", "")),
        api_queries=_split_csv(os.getenv("API_QUERIES", "NGS,next generation sequencing,genomics,precision medicine")),
        news_api_key=os.getenv("NEWS_API_KEY", "").strip(),
        state_db_path=Path(os.getenv("STATE_DB_PATH", str(BASE_DIR / "data" / "news_state.db"))),
        state_retention_days=int(os.getenv("STATE_RETENTION_DAYS", "180")),
        max_links_per_page=int(os.getenv("MAX_LINKS_PER_PAGE", "20")),
        min_request_interval_seconds=float(os.getenv("MIN_REQUEST_INTERVAL_SECONDS", "1.2")),
        max_request_interval_seconds=float(os.getenv("MAX_REQUEST_INTERVAL_SECONDS", "3.5")),
        request_jitter_seconds=float(os.getenv("REQUEST_JITTER_SECONDS", "0.8")),
        max_request_retries=int(os.getenv("MAX_REQUEST_RETRIES", "3")),
        base_retry_backoff_seconds=float(os.getenv("BASE_RETRY_BACKOFF_SECONDS", "2.0")),
        max_retry_backoff_seconds=float(os.getenv("MAX_RETRY_BACKOFF_SECONDS", "20.0")),
        respect_robots_txt=_get_bool("RESPECT_ROBOTS_TXT", True),
        enable_referer=_get_bool("ENABLE_REFERER", True),
        accept_languages=_split_pipe(os.getenv("ACCEPT_LANGUAGES", "en-US,en;q=0.9|en-GB,en;q=0.8|zh-CN,zh;q=0.9,en;q=0.7")),
        feedback_enabled=_get_bool("FEEDBACK_ENABLED", False),
        feedback_imap_host=os.getenv("FEEDBACK_IMAP_HOST", "").strip(),
        feedback_imap_port=int(os.getenv("FEEDBACK_IMAP_PORT", "993")),
        feedback_imap_use_ssl=_get_bool("FEEDBACK_IMAP_USE_SSL", True),
        feedback_email=os.getenv("FEEDBACK_EMAIL", "").strip(),
        feedback_password=os.getenv("FEEDBACK_PASSWORD", "").strip(),
        feedback_mailbox=os.getenv("FEEDBACK_MAILBOX", "INBOX").strip(),
        feedback_search_criteria=os.getenv("FEEDBACK_SEARCH_CRITERIA", "UNSEEN").strip(),
        feedback_mark_seen=_get_bool("FEEDBACK_MARK_SEEN", True),
        feedback_subject_keywords=_split_csv(os.getenv("FEEDBACK_SUBJECT_KEYWORDS", "feedback,digest,日报,新闻")),
        feedback_max_emails_per_run=int(os.getenv("FEEDBACK_MAX_EMAILS_PER_RUN", "20")),
        feedback_parser_provider=os.getenv("FEEDBACK_PARSER_PROVIDER", "rule").strip(),
        feedback_use_llm_parser=_get_bool("FEEDBACK_USE_LLM_PARSER", False),
        feedback_auto_reply=_get_bool("FEEDBACK_AUTO_REPLY", True),
        profile_db_path=Path(os.getenv("PROFILE_DB_PATH", str(BASE_DIR / "data" / "profiles.db"))),
        feedback_log_path=Path(os.getenv("FEEDBACK_LOG_PATH", str(BASE_DIR / "data" / "feedback_log.jsonl"))),
        runtime_state_path=Path(os.getenv("RUNTIME_STATE_PATH", str(BASE_DIR / "data" / "runtime_state.json"))),
        profile_decay_factor=float(os.getenv("PROFILE_DECAY_FACTOR", "0.98")),
        preferred_sources_weight=float(os.getenv("PREFERRED_SOURCES_WEIGHT", "2.0")),
        preferred_keywords_weight=float(os.getenv("PREFERRED_KEYWORDS_WEIGHT", "1.2")),
        negative_keywords_weight=float(os.getenv("NEGATIVE_KEYWORDS_WEIGHT", "-1.0")),
        explicit_article_feedback_weight=float(os.getenv("EXPLICIT_ARTICLE_FEEDBACK_WEIGHT", "4.0")),
        fuzzy_title_match_threshold=float(os.getenv("FUZZY_TITLE_MATCH_THRESHOLD", "0.62")),
        summary_style=os.getenv("SUMMARY_STYLE", "concise").strip(),
        summary_max_chars=int(os.getenv("SUMMARY_MAX_CHARS", "100")),
        summary_focus=_split_csv(os.getenv("SUMMARY_FOCUS", "")),
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip(),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8501")),
        dashboard_debug=_get_bool("DASHBOARD_DEBUG", False),
        dashboard_secret_key=os.getenv("DASHBOARD_SECRET_KEY", "ngs-dashboard-secret").strip(),
    )


def configure_logging(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def validate_settings(settings: Settings) -> None:
    missing = []
    if not settings.llm_api_key:
        missing.append("LLM_API_KEY")
    if not settings.smtp_host:
        missing.append("SMTP_HOST")
    if not settings.smtp_username:
        missing.append("SMTP_USERNAME")
    if not settings.smtp_password:
        missing.append("SMTP_PASSWORD")
    if not settings.smtp_from_email:
        missing.append("SMTP_FROM_EMAIL")
    if not settings.smtp_to:
        missing.append("SMTP_TO")
    if missing:
        raise ValueError(f"缺少必要配置项: {', '.join(missing)}")
