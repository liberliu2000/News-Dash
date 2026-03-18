from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .config import BASE_DIR, configure_logging, load_settings, validate_settings
from .feedback import FeedbackInstruction, FeedbackProcessor
from .fetcher import NewsFetcher, NewsItem
from .mailer import Mailer
from .profile_manager import UserProfile, UserProfileStore
from .runtime_state import RuntimeStateStore
from .summarizer import NewsSummarizer, SummarizedNews

logger = logging.getLogger(__name__)
RUNTIME_STATE_PATH = BASE_DIR / "data" / "runtime_state.json"


def _merge_profiles_for_fetch(profiles: List[UserProfile]) -> UserProfile:
    merged = UserProfile(email="fetch_union")
    for profile in profiles:
        for key, value in profile.preferred_keywords.items():
            merged.preferred_keywords[key] = max(merged.preferred_keywords.get(key, 0.0), value)
        for key, value in profile.preferred_sources.items():
            merged.preferred_sources[key] = max(merged.preferred_sources.get(key, 0.0), value)
        for feed in profile.custom_rss_feeds:
            if feed not in merged.custom_rss_feeds:
                merged.custom_rss_feeds.append(feed)
        for page in profile.custom_web_pages:
            if page not in merged.custom_web_pages:
                merged.custom_web_pages.append(page)
        for endpoint in profile.custom_api_endpoints:
            if endpoint not in merged.custom_api_endpoints:
                merged.custom_api_endpoints.append(endpoint)
        for focus in profile.summary_focus:
            if focus not in merged.summary_focus:
                merged.summary_focus.append(focus)
    return merged


def _serialize_news(item: NewsItem) -> Dict:
    return {
        "news_id": item.news_id,
        "fingerprint": item.fingerprint,
        "title": item.title,
        "source": item.source,
        "link": item.link,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "summary": item.summary,
        "content": item.content[:500],
        "relevance_score": item.relevance_score,
    }


def _serialize_feedback(instruction: FeedbackInstruction) -> Dict:
    return {
        "sender": instruction.sender,
        "subject": instruction.subject,
        "received_at": instruction.received_at,
        "raw_text": instruction.raw_text,
        "parsed_by": instruction.parsed_by,
        "positive_keywords": instruction.positive_keywords,
        "negative_keywords": instruction.negative_keywords,
        "preferred_sources": instruction.preferred_sources,
        "added_rss_feeds": instruction.added_rss_feeds,
        "added_web_pages": instruction.added_web_pages,
        "summary_style": instruction.summary_style,
        "summary_max_chars": instruction.summary_max_chars,
        "article_feedbacks": [
            {
                "article_id": a.article_id,
                "article_link": a.article_link,
                "article_title": a.article_title,
                "vote": a.vote,
                "reason": a.reason,
            }
            for a in instruction.article_feedbacks
        ],
    }


def get_runtime_store() -> RuntimeStateStore:
    return RuntimeStateStore(RUNTIME_STATE_PATH)


def prepare_context():
    settings = load_settings()
    configure_logging(settings.log_level)
    validate_settings(settings)
    profile_store = UserProfileStore(settings)
    recipients = [email.strip().lower() for email in settings.smtp_to if email.strip()]
    profiles = [profile_store.load(email) for email in recipients]
    fetch_profile = _merge_profiles_for_fetch(profiles) if profiles else UserProfile(email="default")
    fetcher = NewsFetcher(settings, profile=fetch_profile)
    return settings, profile_store, recipients, profiles, fetcher


def run_feedback_only() -> List[FeedbackInstruction]:
    settings = load_settings()
    configure_logging(settings.log_level)
    processor = FeedbackProcessor(settings)
    feedbacks = processor.process_feedbacks()
    get_runtime_store().update(
        last_run_status="success",
        pending_feedback_count=0,
        latest_feedback=[_serialize_feedback(item) for item in feedbacks[:20]],
        last_steps=[{"name": "检查反馈邮箱", "status": "done", "count": len(feedbacks)}],
        health="healthy",
        last_error="",
    )
    return feedbacks


def fetch_preview(limit: int = 10) -> List[NewsItem]:
    settings, _, recipients, profiles, fetcher = prepare_context()
    candidate_items = fetcher.fetch_candidates()
    preview_profile = profiles[0] if profiles else UserProfile(email="preview")
    preview_recipient = recipients[0] if recipients else "preview@example.com"
    personalized = fetcher.personalize_for_user(candidate_items, preview_profile, preview_recipient)[:limit]
    get_runtime_store().update(
        today_news_count=len(personalized),
        latest_news=[_serialize_news(item) for item in personalized],
        last_steps=[
            {"name": "抓取新闻", "status": "done", "count": len(candidate_items)},
            {"name": "个性化排序", "status": "done", "count": len(personalized)},
        ],
        health="healthy",
        last_error="",
    )
    return personalized


def send_latest_digest() -> Dict[str, List[SummarizedNews]]:
    settings, _, recipients, profiles, fetcher = prepare_context()
    candidate_items = fetcher.fetch_candidates()
    results: Dict[str, List[SummarizedNews]] = {}
    mailer = Mailer(settings)
    for recipient in recipients:
        profile = next((p for p in profiles if p.email == recipient), UserProfile(email=recipient))
        personalized = fetcher.personalize_for_user(candidate_items, profile, recipient)
        summarized = NewsSummarizer(settings, profile=profile).summarize(personalized) if personalized else []
        if summarized or settings.send_empty_email:
            mailer.send_digest_to(recipient, summarized)
            fetcher.mark_as_sent(recipient, personalized)
        results[recipient] = summarized
    get_runtime_store().update(last_run_status="success", health="healthy", last_error="")
    return results


def run_full_pipeline() -> Dict[str, List[SummarizedNews]]:
    state = get_runtime_store()
    try:
        settings = load_settings()
        configure_logging(settings.log_level)
        validate_settings(settings)
        steps = []
        processor = FeedbackProcessor(settings)
        feedbacks = processor.process_feedbacks()
        steps.append({"name": "检查反馈邮箱", "status": "done", "count": len(feedbacks)})
        profile_store = UserProfileStore(settings)
        recipients = [email.strip().lower() for email in settings.smtp_to if email.strip()]
        profiles = [profile_store.load(email) for email in recipients]
        fetch_profile = _merge_profiles_for_fetch(profiles) if profiles else UserProfile(email="default")
        fetcher = NewsFetcher(settings, profile=fetch_profile)
        candidate_items = fetcher.fetch_candidates()
        steps.append({"name": "抓取新闻", "status": "done", "count": len(candidate_items)})
        results: Dict[str, List[SummarizedNews]] = {}
        preview_news: List[Dict] = []
        mailer = Mailer(settings)
        for recipient in recipients:
            profile = profile_store.load(recipient)
            personalized_items = fetcher.personalize_for_user(candidate_items, profile, recipient)
            summarized = NewsSummarizer(settings, profile=profile).summarize(personalized_items) if personalized_items else []
            if summarized or settings.send_empty_email:
                mailer.send_digest_to(recipient, summarized)
                fetcher.mark_as_sent(recipient, personalized_items)
            results[recipient] = summarized
            if not preview_news:
                preview_news = [
                    {
                        **_serialize_news(s.item),
                        "summary": s.summary,
                    }
                    for s in summarized[:10]
                ]
        steps.append({"name": "摘要与发送邮件", "status": "done", "count": sum(len(v) for v in results.values())})
        state.update(
            last_run_at=datetime.now(timezone.utc).isoformat(),
            last_run_status="success",
            last_error="",
            today_news_count=len(preview_news),
            pending_feedback_count=0,
            latest_news=preview_news,
            latest_feedback=[_serialize_feedback(item) for item in feedbacks[:20]],
            last_steps=steps,
            health="healthy",
        )
        return results
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline 运行失败: %s", exc)
        state.update(
            last_run_at=datetime.now(timezone.utc).isoformat(),
            last_run_status="error",
            last_error=str(exc),
            health="abnormal",
        )
        raise
