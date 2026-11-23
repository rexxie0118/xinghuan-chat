"""Per-user long-term memory.

This is intentionally separate from the KB-wide ``memories`` table: user
memories are private, scoped by ``user_id``, and never become document
evidence. They are recalled as soft context and can be forgotten by the user.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kb.textutil import embed_tokens

from .db import connect
from .util import iso, new_id

VALID_KINDS = {"fact", "preference", "task"}
MAX_CONTENT_CHARS = 300

_EXPLICIT = re.compile(r"(?:请)?记住[：:，,\s]*(.+)", re.S)
_NAME = re.compile(r"(?:我叫|我的名字是)\s*([^\s，。,.!?！？]{1,30})")
_PREFERENCE = re.compile(r"我(?:更|比较)?(?:喜欢|偏好)\s*([^。！？\n]{1,120})")
_ADDRESS = re.compile(r"以后(?:请)?(?:用|回答用|叫我)\s*([^。！？\n]{1,60})")


def extract_candidates(text: str) -> list[dict[str, Any]]:
    text = " ".join((text or "").split()).strip()
    if not text or len(text) > 500:
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, content: str, *, subject="", predicate="", object_text="") -> None:
        content = content.strip()[:MAX_CONTENT_CHARS]
        key = re.sub(r"\s+", "", content).lower()
        if content and key not in seen:
            seen.add(key)
            candidates.append(
                {
                    "kind": kind,
                    "content": content,
                    "subject": subject,
                    "predicate": predicate,
                    "object_text": object_text or content,
                    "importance": 0.8 if kind == "preference" else 0.7,
                    "confidence": 0.8,
                }
            )

    explicit = _EXPLICIT.search(text)
    if explicit:
        content = explicit.group(1).strip("。；; ")
        if content:
            add("preference" if "喜欢" in content or "偏好" in content else "fact", content)
    name = _NAME.search(text)
    if name:
        add("fact", f"我的名字是{name.group(1)}", subject="用户", predicate="名字", object_text=name.group(1))
    pref = _PREFERENCE.search(text)
    if pref:
        value = pref.group(1).strip("。；; ")
        if value:
            add("preference", f"我偏好：{value}", subject="用户", predicate="偏好", object_text=value)
    address = _ADDRESS.search(text)
    if address:
        value = address.group(1).strip("。；; ")
        if value:
            add("preference", f"称呼偏好：{value}", subject="用户", predicate="称呼", object_text=value)
    return candidates


class UserMemoryService:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def remember(
        self,
        user_id: str,
        *,
        kind: str,
        content: str,
        subject: str = "",
        predicate: str = "",
        object_text: str = "",
        importance: float = 0.6,
        confidence: float = 0.7,
        source_conversation_id: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        if kind not in VALID_KINDS:
            raise ValueError(f"未知记忆类型：{kind}")
        content = " ".join((content or "").split())[:MAX_CONTENT_CHARS]
        if not content:
            raise ValueError("记忆内容不能为空")
        subject = (subject or "").strip()[:80]
        predicate = (predicate or "").strip()[:80]
        object_text = (object_text or content).strip()[:MAX_CONTENT_CHARS]
        now = iso()
        conn = self._conn()
        try:
            with conn:
                existing = None
                if subject and predicate:
                    existing = conn.execute(
                        "SELECT * FROM user_memories WHERE user_id=? AND valid_to IS NULL"
                        " AND subject=? AND predicate=? AND object_text=?",
                        (user_id, subject, predicate, object_text),
                    ).fetchone()
                else:
                    existing = conn.execute(
                        "SELECT * FROM user_memories WHERE user_id=? AND valid_to IS NULL"
                        " AND content=?",
                        (user_id, content),
                    ).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE user_memories SET hits=hits+1, last_used_at=?, updated_at=?"
                        " WHERE id=?",
                        (now, now, existing["id"]),
                    )
                    return {"status": "duplicate", "id": existing["id"]}

                memory_id = new_id("umem")
                superseded: list[str] = []
                if subject and predicate:
                    for row in conn.execute(
                        "SELECT id FROM user_memories WHERE user_id=? AND valid_to IS NULL"
                        " AND subject=? AND predicate=?",
                        (user_id, subject, predicate),
                    ).fetchall():
                        conn.execute(
                            "UPDATE user_memories SET valid_to=?, superseded_by=?, updated_at=?"
                            " WHERE id=?",
                            (now, memory_id, now, row["id"]),
                        )
                        superseded.append(row["id"])

                conn.execute(
                    """
                    INSERT INTO user_memories(id, user_id, kind, content, subject,
                        predicate, object_text, importance, confidence, hits,
                        source_conversation_id, source_message_id, created_at,
                        updated_at, last_used_at, valid_to, superseded_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        memory_id,
                        user_id,
                        kind,
                        content,
                        subject,
                        predicate,
                        object_text,
                        max(0.0, min(1.0, float(importance))),
                        max(0.0, min(1.0, float(confidence))),
                        0,
                        source_conversation_id,
                        source_message_id,
                        now,
                        now,
                        now,
                        None,
                        "",
                    ),
                )
        finally:
            conn.close()
        return {"status": "superseded" if superseded else "created", "id": memory_id, "superseded": superseded}

    def extract_and_remember(
        self,
        user_id: str,
        text: str,
        *,
        conversation_id: str = "",
        message_id: str = "",
    ) -> list[dict[str, Any]]:
        results = []
        for candidate in extract_candidates(text):
            results.append(
                self.remember(
                    user_id,
                    source_conversation_id=conversation_id,
                    source_message_id=message_id,
                    **candidate,
                )
            )
        return results

    def recall(self, user_id: str, query: str, *, k: int = 5) -> list[dict[str, Any]]:
        query = (query or "").strip()
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM user_memories WHERE user_id=? AND valid_to IS NULL"
                " ORDER BY last_used_at DESC LIMIT 200",
                (user_id,),
            ).fetchall()
            if not rows:
                return []
            q_tokens = {t for t, _ in embed_tokens(query)}
            scored = []
            now = datetime.now(timezone.utc)
            for row in rows:
                item = dict(row)
                tokens = {t for t, _ in embed_tokens(item["content"])}
                lexical = (
                    len(q_tokens & tokens) / len(q_tokens | tokens)
                    if q_tokens and tokens
                    else 0.0
                )
                try:
                    last = datetime.fromisoformat(item["last_used_at"])
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    idle_days = max(0.0, (now - last).total_seconds() / 86400)
                except (TypeError, ValueError):
                    idle_days = 0.0
                if lexical <= 0 and item["kind"] != "preference":
                    # Facts are only useful when the current question shares a
                    # term; preferences apply across topics.
                    continue
                recency = math.exp(-0.01 * idle_days)
                score = 0.65 * lexical + 0.25 * float(item["importance"]) + 0.10 * recency
                item["score"] = round(score, 4)
                scored.append(item)
            scored.sort(key=lambda x: (-x["score"], x["last_used_at"]))
            top = scored[: max(1, min(int(k), 12))]
            if top:
                ts = iso()
                with conn:
                    conn.executemany(
                        "UPDATE user_memories SET hits=hits+1, last_used_at=? WHERE id=?",
                        [(ts, item["id"]) for item in top],
                    )
                for item in top:
                    item["hits"] = int(item["hits"]) + 1
            return top
        finally:
            conn.close()

    def list(self, user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM user_memories WHERE user_id=? AND valid_to IS NULL"
                " ORDER BY created_at DESC LIMIT ?",
                (user_id, max(1, min(int(limit), 200))),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def forget(self, user_id: str, memory_id: str) -> bool:
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM user_memories WHERE id=? AND user_id=?", (memory_id, user_id)
                )
            return cur.rowcount > 0
        finally:
            conn.close()
