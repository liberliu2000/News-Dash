from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from flask import Flask, flash, redirect, render_template, request, url_for

from .config import BASE_DIR, load_settings
from .env_manager import csv_join, mask_env_values, read_env_file, write_env_updates
from .feedback import FeedbackInstruction
from .pipeline_service import fetch_preview, get_runtime_store, run_feedback_only, run_full_pipeline, send_latest_digest
from .profile_manager import UserProfile, UserProfileStore


app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = load_settings().dashboard_secret_key


def _safe_settings():
    return load_settings()


def _profile_store() -> UserProfileStore:
    return UserProfileStore(_safe_settings())


def _runtime_context() -> Dict:
    settings = _safe_settings()
    state = get_runtime_store().load()
    store = _profile_store()
    env_values = read_env_file()
    masked_env = mask_env_values(env_values)
    recipients = settings.smtp_to or [p.email for p in store.list_profiles()]
    profiles = store.list_profiles(recipients)
    selected_email = request.args.get("email") or (profiles[0].email if profiles else (recipients[0] if recipients else ""))
    selected_profile = store.load(selected_email) if selected_email else UserProfile(email="")
    feedback_date = request.args.get("feedback_date", "")
    feedback_events = store.list_feedback_events(limit=100, date_prefix=feedback_date)
    latest_news = state.latest_news or []
    overview = {
        "today_news_count": state.today_news_count,
        "pending_feedback_count": state.pending_feedback_count,
        "last_run_time": state.last_run_at,
        "health": state.health,
        "status": state.last_run_status,
        "last_error": state.last_error,
    }
    config_groups = {
        "llm": ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "MODEL_PROVIDER", "LLM_TEMPERATURE", "LLM_MAX_TOKENS"],
        "smtp": ["SMTP_HOST", "SMTP_PORT", "SMTP_USE_SSL", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_NAME", "SMTP_FROM_EMAIL", "SMTP_TO", "SMTP_CC", "SMTP_BCC"],
        "feedback": ["FEEDBACK_ENABLED", "FEEDBACK_IMAP_HOST", "FEEDBACK_IMAP_PORT", "FEEDBACK_IMAP_USE_SSL", "FEEDBACK_EMAIL", "FEEDBACK_PASSWORD", "FEEDBACK_SEARCH_CRITERIA", "FEEDBACK_MAILBOX", "FEEDBACK_AUTO_REPLY"],
        "sources": ["RSS_FEEDS", "WEB_PAGES", "NEWS_API_ENDPOINTS", "NEWS_API_KEY", "API_QUERIES"],
        "weights": ["PREFERRED_SOURCES_WEIGHT", "PREFERRED_KEYWORDS_WEIGHT", "NEGATIVE_KEYWORDS_WEIGHT", "PROFILE_DECAY_FACTOR", "EXPLICIT_ARTICLE_FEEDBACK_WEIGHT", "FUZZY_TITLE_MATCH_THRESHOLD"],
    }
    source_items = []
    for source_type, env_key in [("rss", "RSS_FEEDS"), ("web", "WEB_PAGES"), ("api", "NEWS_API_ENDPOINTS")]:
        for idx, value in enumerate([item.strip() for item in env_values.get(env_key, "").split(",") if item.strip()]):
            source_items.append({"source_type": source_type, "env_key": env_key, "value": value, "idx": idx})
    return {
        "settings": settings,
        "state": state,
        "overview": overview,
        "env_values": env_values,
        "masked_env": masked_env,
        "config_groups": config_groups,
        "source_items": source_items,
        "profiles": profiles,
        "selected_profile": selected_profile,
        "feedback_events": feedback_events,
        "feedback_date": feedback_date,
        "latest_news": latest_news,
        "summary_styles": ["concise", "detailed", "technical"],
        "recipients": recipients,
        "steps": state.last_steps or [],
        "now": datetime.now(),
    }


@app.route("/")
def dashboard():
    return render_template("dashboard.html", **_runtime_context())


@app.post("/config/save/<group>")
def save_config_group(group: str):
    ctx = _runtime_context()
    keys = ctx["config_groups"].get(group, [])
    updates = {}
    env_values = read_env_file()
    for key in keys:
        value = request.form.get(key, "")
        if key in {"LLM_API_KEY", "SMTP_PASSWORD", "FEEDBACK_PASSWORD", "NEWS_API_KEY"} and not value:
            value = env_values.get(key, "")
        updates[key] = value
    write_env_updates(updates)
    flash(f"{group} 配置已保存", "success")
    return redirect(url_for("dashboard"))


@app.post("/sources/add")
def add_source():
    env_key = request.form.get("env_key", "RSS_FEEDS")
    new_value = request.form.get("value", "").strip()
    env_values = read_env_file()
    existing = [item.strip() for item in env_values.get(env_key, "").split(",") if item.strip()]
    if new_value and new_value not in existing:
        existing.append(new_value)
        write_env_updates({env_key: csv_join(existing)})
        flash("抓取源已新增", "success")
    else:
        flash("抓取源为空或已存在", "warning")
    return redirect(url_for("dashboard") + "#sources")


@app.post("/sources/delete")
def delete_source():
    env_key = request.form.get("env_key", "RSS_FEEDS")
    idx = int(request.form.get("idx", "-1"))
    env_values = read_env_file()
    existing = [item.strip() for item in env_values.get(env_key, "").split(",") if item.strip()]
    if 0 <= idx < len(existing):
        existing.pop(idx)
        write_env_updates({env_key: csv_join(existing)})
        flash("抓取源已删除", "success")
    else:
        flash("未找到抓取源", "warning")
    return redirect(url_for("dashboard") + "#sources")


@app.post("/sources/test")
def test_source():
    value = request.form.get("value", "")
    source_type = request.form.get("source_type", "rss")
    examples = {
        "rss": [f"[模拟] 从 RSS 抓到：Single-cell sequencing workflow update", f"[模拟] 从 RSS 抓到：NGS reagent launch from {value[:40]}"],
        "web": [f"[模拟] 网页解析成功：Nature-like article title from {value[:40]}", "[模拟] 提取到 7 个候选文章链接"],
        "api": [f"[模拟] API 返回：Spatial transcriptomics funding update", "[模拟] API 返回：Long-read sequencing clinical paper"],
    }
    flash("测试抓取结果：" + "；".join(examples.get(source_type, examples["rss"])), "info")
    return redirect(url_for("dashboard") + "#sources")


@app.post("/profiles/save")
def save_profile():
    store = _profile_store()
    email = request.form.get("email", "").strip().lower()
    profile = store.load(email)
    profile.summary_style = request.form.get("summary_style", "concise")
    profile.summary_max_chars = int(request.form.get("summary_max_chars", "100") or 100)
    profile.summary_focus = [item.strip() for item in request.form.get("summary_focus", "").split(",") if item.strip()]
    profile.preferred_keywords = _parse_weight_mapping(request.form.get("preferred_keywords", ""))
    profile.negative_keywords = _parse_weight_mapping(request.form.get("negative_keywords", ""))
    profile.preferred_sources = _parse_weight_mapping(request.form.get("preferred_sources", ""))
    profile.custom_rss_feeds = _parse_lines(request.form.get("custom_rss_feeds", ""))
    profile.custom_web_pages = _parse_lines(request.form.get("custom_web_pages", ""))
    profile.custom_api_endpoints = _parse_lines(request.form.get("custom_api_endpoints", ""))
    store.save(profile)
    flash(f"用户画像已保存：{email}", "success")
    return redirect(url_for("dashboard", email=email) + "#profiles")


@app.post("/run/<mode>")
def run_action(mode: str):
    try:
        if mode == "full":
            run_full_pipeline()
            flash("完整流程已执行：抓取 → 摘要 → 排序 → 发送", "success")
        elif mode == "feedback":
            feedbacks = run_feedback_only()
            flash(f"反馈邮箱检查完成，共处理 {len(feedbacks)} 条反馈", "success")
        elif mode == "fetch":
            items = fetch_preview()
            flash(f"仅抓取完成，当前预览新闻 {len(items)} 条", "success")
        elif mode == "send":
            result = send_latest_digest()
            flash(f"仅发送完成，共发送给 {len(result)} 个用户", "success")
        else:
            flash("未知操作", "warning")
    except Exception as exc:  # noqa: BLE001
        flash(f"执行失败：{exc}", "danger")
    return redirect(url_for("dashboard") + "#run")


@app.post("/news-feedback")
def news_feedback():
    settings = _safe_settings()
    store = _profile_store()
    recipient = request.form.get("recipient", "").strip().lower() or (settings.smtp_to[0] if settings.smtp_to else "")
    fingerprint = request.form.get("fingerprint", "").strip()
    title = request.form.get("title", "").strip()
    link = request.form.get("link", "").strip()
    vote = int(request.form.get("vote", "0") or 0)
    if recipient and fingerprint and vote in {-1, 1}:
        store.record_article_feedback(recipient, fingerprint, vote, article_title=title, article_link=link, reason="dashboard_manual_feedback")
        instruction = FeedbackInstruction(
            sender=recipient,
            subject="Dashboard manual feedback",
            received_at=datetime.now().isoformat(),
            raw_text=f"{'点赞' if vote > 0 else '点踩'} {title}",
            article_feedbacks=[],
            parsed_by="dashboard",
        )
        store.log_feedback_event(recipient, "", instruction.received_at, instruction.subject, instruction.parsed_by, instruction.raw_text, json.dumps({"vote": vote, "title": title, "link": link}, ensure_ascii=False))
        flash("已记录该新闻的相关性反馈，并将用于后续排序", "success")
    else:
        flash("反馈记录失败，缺少必要信息", "danger")
    return redirect(url_for("dashboard") + "#news")


def _parse_weight_mapping(text: str) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
        elif "=" in line:
            key, value = line.split("=", 1)
        else:
            key, value = line, "1.0"
        key = key.strip()
        if not key:
            continue
        try:
            result[key] = float(value.strip() or 1.0)
        except ValueError:
            result[key] = 1.0
    return result


def _parse_lines(text: str) -> List[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


if __name__ == "__main__":
    settings = load_settings()
    app.run(host=settings.dashboard_host, port=settings.dashboard_port, debug=settings.dashboard_debug)
