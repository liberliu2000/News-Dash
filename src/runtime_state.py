from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class RuntimeState:
    last_run_at: Optional[str] = None
    last_run_status: str = "idle"
    last_error: str = ""
    today_news_count: int = 0
    pending_feedback_count: int = 0
    health: str = "healthy"
    latest_news: List[Dict[str, Any]] = field(default_factory=list)
    latest_feedback: List[Dict[str, Any]] = field(default_factory=list)
    last_steps: List[Dict[str, Any]] = field(default_factory=list)


class RuntimeStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RuntimeState:
        if not self.path.exists():
            return RuntimeState()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return RuntimeState(**data)
        except Exception:
            return RuntimeState()

    def save(self, state: RuntimeState) -> None:
        self.path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")

    def update(self, **kwargs: Any) -> RuntimeState:
        state = self.load()
        for key, value in kwargs.items():
            setattr(state, key, value)
        if not kwargs.get("last_run_at"):
            state.last_run_at = datetime.now(timezone.utc).isoformat()
        self.save(state)
        return state
