from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .config import Settings

logger = logging.getLogger(__name__)


@dataclass
class UserProfile:
    email: str = ""
    preferred_keywords: Dict[str, float] = field(default_factory=dict)
    negative_keywords: Dict[str, float] = field(default_factory=dict)
    preferred_sources: Dict[str, float] = field(default_factory=dict)
    custom_rss_feeds: List[str] = field(default_factory=list)
    custom_web_pages: List[str] = field(default_factory=list)
    custom_api_endpoints: List[str] = field(default_factory=list)
    summary_style: str = "concise"
    summary_max_chars: int = 100
    summary_focus: List[str] = field(default_factory=list)
    explicit_article_feedback: Dict[str, float] = field(default_factory=dict)
    feedback_history_count: int = 0
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class UserProfileStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db_path = Path(settings.profile_db_path)
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
                CREATE TABLE IF NOT EXISTS user_profiles (
                    email TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    message_id TEXT,
                    received_at TEXT NOT NULL,
                    subject TEXT,
                    parsed_by TEXT,
                    raw_text TEXT,
                    instruction_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS article_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    article_fingerprint TEXT NOT NULL,
                    article_title TEXT,
                    article_link TEXT,
                    vote INTEGER NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_feedback_messages (
                    message_id TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _default_profile(self, email: str) -> UserProfile:
        return UserProfile(
            email=email,
            summary_style=self.settings.summary_style,
            summary_max_chars=self.settings.summary_max_chars,
            summary_focus=list(self.settings.summary_focus),
        )

    def load(self, email: str) -> UserProfile:
        email = (email or "").strip().lower()
        with self._connect() as conn:
            row = conn.execute("SELECT profile_json FROM user_profiles WHERE email = ?", (email,)).fetchone()
        if row is None:
            profile = self._default_profile(email)
        else:
            try:
                data = json.loads(row["profile_json"])
                profile = UserProfile(
                    email=email,
                    preferred_keywords=data.get("preferred_keywords", {}),
                    negative_keywords=data.get("negative_keywords", {}),
                    preferred_sources=data.get("preferred_sources", {}),
                    custom_rss_feeds=data.get("custom_rss_feeds", []),
                    custom_web_pages=data.get("custom_web_pages", []),
                    custom_api_endpoints=data.get("custom_api_endpoints", []),
                    summary_style=data.get("summary_style", self.settings.summary_style),
                    summary_max_chars=int(data.get("summary_max_chars", self.settings.summary_max_chars)),
                    summary_focus=data.get("summary_focus", list(self.settings.summary_focus)),
                    explicit_article_feedback=data.get("explicit_article_feedback", {}),
                    feedback_history_count=int(data.get("feedback_history_count", 0)),
                    updated_at=data.get("updated_at", datetime.now(timezone.utc).isoformat()),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取用户画像失败，将回退默认画像: %s", exc)
                profile = self._default_profile(email)
        profile.explicit_article_feedback = self.load_article_feedback(email)
        return profile

    def list_profiles(self, emails: Optional[Iterable[str]] = None) -> List[UserProfile]:
        email_list = [e.strip().lower() for e in (emails or []) if e.strip()]
        if not email_list:
            with self._connect() as conn:
                rows = conn.execute("SELECT email FROM user_profiles ORDER BY email").fetchall()
            email_list = [row["email"] for row in rows]
        return [self.load(email) for email in email_list]

    def save(self, profile: UserProfile) -> None:
        profile.updated_at = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(asdict(profile), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_profiles (email, profile_json, updated_at) VALUES (?, ?, ?)",
                (profile.email.strip().lower(), payload, profile.updated_at),
            )
            conn.commit()

    def apply_decay(self, profile: UserProfile) -> UserProfile:
        decay = self.settings.profile_decay_factor
        if decay <= 0 or decay >= 1:
            return profile
        for mapping_name in ["preferred_keywords", "negative_keywords", "preferred_sources"]:
            mapping = getattr(profile, mapping_name)
            updated = {k: round(v * decay, 4) for k, v in mapping.items() if abs(v * decay) >= 0.05}
            setattr(profile, mapping_name, updated)
        return profile

    def merge_unique(self, current: List[str], incoming: List[str]) -> List[str]:
        seen = {item.strip().lower() for item in current if item.strip()}
        result = [item for item in current if item.strip()]
        for item in incoming:
            normalized = item.strip()
            lowered = normalized.lower()
            if normalized and lowered not in seen:
                seen.add(lowered)
                result.append(normalized)
        return result

    def log_feedback_event(self, email: str, message_id: str, received_at: str, subject: str, parsed_by: str, raw_text: str, instruction_json: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback_events (email, message_id, received_at, subject, parsed_by, raw_text, instruction_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (email.strip().lower(), message_id, received_at, subject, parsed_by, raw_text, instruction_json),
            )
            conn.commit()

    def list_feedback_events(self, limit: int = 100, date_prefix: str = "") -> List[Dict]:
        query = "SELECT email, message_id, received_at, subject, parsed_by, raw_text, instruction_json FROM feedback_events"
        params: List[str] = []
        if date_prefix:
            query += " WHERE substr(received_at, 1, 10) = ?"
            params.append(date_prefix)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(str(limit))
        with self._connect() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["instruction_json"])
            except Exception:
                payload = {}
            result.append(
                {
                    "email": row["email"],
                    "message_id": row["message_id"],
                    "received_at": row["received_at"],
                    "subject": row["subject"],
                    "parsed_by": row["parsed_by"],
                    "raw_text": row["raw_text"],
                    "instruction": payload,
                }
            )
        return result

    def is_message_processed(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM processed_feedback_messages WHERE message_id = ?", (message_id,)).fetchone()
        return row is not None

    def mark_message_processed(self, message_id: str) -> None:
        if not message_id:
            return
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO processed_feedback_messages (message_id, processed_at) VALUES (?, ?)",
                (message_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()

    def record_article_feedback(self, email: str, article_fingerprint: str, vote: int, article_title: str = "", article_link: str = "", reason: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO article_feedback (email, article_fingerprint, article_title, article_link, vote, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email.strip().lower(),
                    article_fingerprint,
                    article_title,
                    article_link,
                    int(vote),
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()

    def load_article_feedback(self, email: str) -> Dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT article_fingerprint, SUM(vote) AS score FROM article_feedback WHERE email = ? GROUP BY article_fingerprint",
                (email.strip().lower(),),
            ).fetchall()
        return {row["article_fingerprint"]: float(row["score"] or 0.0) for row in rows}
