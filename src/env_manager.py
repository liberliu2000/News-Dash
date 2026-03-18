from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Iterable

from .config import ENV_PATH


MASK_KEYS = {"LLM_API_KEY", "SMTP_PASSWORD", "FEEDBACK_PASSWORD", "NEWS_API_KEY"}


def read_env_file(path: Path | None = None) -> Dict[str, str]:
    env_path = Path(path or ENV_PATH)
    values: Dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env_updates(updates: Dict[str, str], path: Path | None = None) -> None:
    env_path = Path(path or ENV_PATH)
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated_keys = set(updates)
    result = []
    seen = set()
    pattern_cache = {key: re.compile(rf"^\s*{re.escape(key)}=") for key in updates}
    for line in lines:
        replaced = False
        for key, pattern in pattern_cache.items():
            if pattern.match(line):
                result.append(f"{key}={updates[key]}")
                seen.add(key)
                replaced = True
                break
        if not replaced:
            result.append(line)
    for key in updates:
        if key not in seen:
            result.append(f"{key}={updates[key]}")
    env_path.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")


def mask_env_values(data: Dict[str, str]) -> Dict[str, str]:
    masked = dict(data)
    for key in MASK_KEYS:
        value = masked.get(key, "")
        if value:
            masked[key] = value[:4] + "*" * max(0, len(value) - 4)
    return masked


def csv_join(items: Iterable[str]) -> str:
    return ",".join([item.strip() for item in items if item.strip()])
