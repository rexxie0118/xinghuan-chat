"""Small shared helpers."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).isoformat(timespec="microseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(9)}"


def expires_at(days: int) -> str:
    return iso(utcnow() + timedelta(days=max(1, days)))


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def conversation_title(text: str, limit: int = 22) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return "新的对话"
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip("，。！？,.!?;；:： ") + "…"
