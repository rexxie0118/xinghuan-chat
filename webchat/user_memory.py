"""Per-user long-term memory with write quality and lifecycle controls.

This is intentionally separate from the KB-wide ``memories`` table: user
memories are private, scoped by ``user_id``, and never become document
evidence. They are recalled as soft context and can be forgotten by the user.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kb.textutil import embed_tokens

from .db import connect
from .util import iso, new_id

LOG = logging.getLogger("webchat.memory")

VALID_KINDS = {"fact", "preference", "task"}
VALID_SCOPES = {"personal", "department", "org"}
VALID_STATUSES = {"active", "pending", "rejected", "archived"}
MAX_CONTENT_CHARS = 300
MIN_AUTO_CONFIDENCE = 0.6
NEAR_DUPLICATE = 0.90
SEMANTIC_CANDIDATE = 0.15
MAX_RECALL_CHARS = 1200

_EXPLICIT = re.compile(r"(?:请)?记住[：:，,\s]*(.+)", re.S)
_NAME = re.compile(r"(?:我叫|我的名字是)\s*([^\s，。,.!?！？]{1,30})")
_PREFERENCE = re.compile(r"我(?:更|比较)?(?:喜欢|偏好)\s*([^。！？\n]{1,120})")
_ADDRESS = re.compile(r"以后(?:请)?(?:用|回答用|叫我)\s*([^。！？\n]{1,60})")


def _normalise(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def _similarity(left: str, right: str) -> float:
    a = {token for token, _ in embed_tokens(_normalise(left))}
    b = {token for token, _ in embed_tokens(_normalise(right))}
    if not a or not b:
        return 1.0 if _normalise(left) == _normalise(right) and _normalise(left) else 0.0
    return len(a & b) / len(a | b)


def _quality(candidate: dict[str, Any]) -> tuple[bool, str]:
    content = str(candidate.get("content") or "").strip()
    if len(_normalise(content)) < 3:
        return False, "too_short"
    if float(candidate.get("confidence") or 0) < MIN_AUTO_CONFIDENCE:
        return False, "low_confidence"
    return True, ""


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


class MemoryConflict(RuntimeError):
    """A lower-authority approval cannot override a higher-authority memory."""


class UserMemoryService:
    def __init__(self, db_path: Path | str, *, semantic_judge: Any = None) -> None:
        self.db_path = Path(db_path)
        self.semantic_judge = semantic_judge

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def _semantically_same(self, left: str, right: str, kind: str) -> bool:
        if self.semantic_judge is None:
            return False
        try:
            return bool(self.semantic_judge.same(left, right, kind))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("semantic memory merge failed: %s", exc)
            return False

    def _semantic_relation(self, left: str, right: str) -> str:
        judge = self.semantic_judge
        if judge is None:
            return "different"
        try:
            if hasattr(judge, "relation"):
                return str(judge.relation(left, right))
            return "same" if judge.same(left, right) else "different"
        except Exception as exc:  # noqa: BLE001
            LOG.warning("semantic relation failed: %s", exc)
            return "different"

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
        scope: str = "personal",
        scope_id: str = "",
        agent_id: str = "",
        source_uri: str = "",
        evidence_snippet: str = "",
        status: str = "active",
        auto: bool = False,
    ) -> dict[str, Any]:
        if kind not in VALID_KINDS:
            raise ValueError(f"未知记忆类型：{kind}")
        if scope not in VALID_SCOPES:
            raise ValueError(f"未知记忆范围：{scope}")
        if status not in VALID_STATUSES:
            raise ValueError(f"未知记忆状态：{status}")
        if scope != "personal" and status == "active":
            status = "pending"
        agent_id = str(agent_id or "").strip()[:80]
        if scope == "personal":
            scope_id = user_id
        elif not scope_id:
            raise ValueError("department/org 记忆必须指定 scope_id")
        content = " ".join((content or "").split())[:MAX_CONTENT_CHARS]
        if not content:
            raise ValueError("记忆内容不能为空")
        subject = (subject or "").strip()[:80]
        predicate = (predicate or "").strip()[:80]
        object_text = (object_text or content).strip()[:MAX_CONTENT_CHARS]
        if auto:
            ok, reason = _quality(
                {
                    "content": content,
                    "confidence": confidence,
                }
            )
            if not ok:
                return {"status": "skipped", "reason": reason}
        now = iso()
        conn = self._conn()
        try:
            with conn:
                if scope == "personal":
                    active = conn.execute(
                        "SELECT * FROM user_memories WHERE user_id=? AND scope='personal'"
                        " AND valid_to IS NULL AND status IN ('active','pending')",
                        (user_id,),
                    ).fetchall()
                else:
                    active = conn.execute(
                        "SELECT * FROM user_memories WHERE scope=? AND scope_id=?"
                        " AND valid_to IS NULL AND status IN ('active','pending')",
                        (scope, scope_id),
                    ).fetchall()

                # 1. Near-duplicate / exact duplicate: merge into the existing row.
                for row in active:
                    similarity = _similarity(row["content"], content)
                    semantic = (
                        similarity >= SEMANTIC_CANDIDATE
                        and row["kind"] == kind
                        and self._semantically_same(row["content"], content, kind)
                    )
                    duplicate = (
                        (subject and predicate and row["subject"] == subject
                         and row["predicate"] == predicate and row["object_text"] == object_text)
                        or _normalise(row["content"]) == _normalise(content)
                        or similarity >= NEAR_DUPLICATE
                        or semantic
                    )
                    if not duplicate:
                        continue
                    conn.execute(
                        "UPDATE user_memories SET hits=hits+1, last_used_at=?, updated_at=?,"
                        " importance=CASE WHEN importance > ? THEN importance ELSE ? END,"
                        " confidence=CASE WHEN confidence > ? THEN confidence ELSE ? END"
                        " WHERE id=?",
                        (now, now, float(importance), float(importance), float(confidence), float(confidence), row["id"]),
                    )
                    return {"status": "duplicate", "id": row["id"], "snapshot": dict(row)}

                memory_id = new_id("umem")
                superseded: list[str] = []
                superseded_snapshots: list[dict[str, Any]] = []
                if scope == "personal" and subject and predicate:
                    for row in active:
                        if row["subject"] != subject or row["predicate"] != predicate:
                            continue
                        superseded_snapshots.append(dict(row))
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
                        updated_at, last_used_at, valid_from, scope, scope_id,
                        agent_id, source_uri, evidence_snippet, created_by, status,
                        approved_by, approved_at, authority, valid_to, superseded_by)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                        now,
                        scope,
                        scope_id,
                        agent_id,
                        source_uri or (f"conversation:{source_conversation_id}" if source_conversation_id else ""),
                        evidence_snippet[:MAX_CONTENT_CHARS],
                        user_id,
                        status,
                        "",
                        "",
                        0,
                        None,
                        "",
                    ),
                )
        finally:
            conn.close()
        snapshot = self.snapshot(memory_id)
        return {
            "status": "superseded" if superseded else "created",
            "id": memory_id,
            "superseded": superseded,
            "superseded_snapshots": superseded_snapshots,
            "snapshot": snapshot,
        }

    def extract_and_remember(
        self,
        user_id: str,
        text: str,
        *,
        conversation_id: str = "",
        message_id: str = "",
        agent_id: str = "",
    ) -> list[dict[str, Any]]:
        results = []
        for candidate in extract_candidates(text):
            results.append(
                self.remember(
                    user_id,
                    source_conversation_id=conversation_id,
                    source_message_id=message_id,
                    source_uri=f"conversation:{conversation_id}" if conversation_id else "",
                    evidence_snippet=text,
                    agent_id=agent_id,
                    auto=True,
                    **candidate,
                )
            )
        return results

    @staticmethod
    def _visible_clause(
        user_id: str, user: dict[str, Any] | None, *, agent_id: str | None = None
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []

        def add(condition: str, values: list[Any]) -> None:
            if agent_id is not None:
                condition = f"({condition}) AND (agent_id='' OR agent_id=?)"
                values = values + [agent_id]
            conditions.append(condition)
            params.extend(values)

        add("user_id=? AND scope='personal'", [user_id])
        if user:
            role = str(user.get("role") or "member")
            department = str(user.get("department") or "")
            if role == "admin":
                add("scope='department'", [])
            elif department:
                add("scope='department' AND scope_id=?", [department])
            add("scope='org'", [])
        return "(" + " OR ".join(conditions) + ")", params

    def recall(
        self,
        user_id: str,
        query: str,
        *,
        k: int = 5,
        max_chars: int = MAX_RECALL_CHARS,
        user: dict[str, Any] | None = None,
        agent_id: str = "",
    ) -> list[dict[str, Any]]:
        query = (query or "").strip()
        conn = self._conn()
        try:
            visible, params = self._visible_clause(user_id, user, agent_id=agent_id)
            rows = conn.execute(
                "SELECT * FROM user_memories WHERE " + visible
                + " AND valid_to IS NULL AND status='active'"
                " ORDER BY last_used_at DESC LIMIT 200",
                tuple(params),
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
                    continue
                recency = math.exp(-0.01 * idle_days)
                score = (
                    0.55 * lexical
                    + 0.20 * float(item["importance"])
                    + 0.15 * float(item["confidence"])
                    + 0.10 * recency
                    + (0.03 if item["kind"] == "preference" else 0.0)
                )
                item["score"] = round(score, 4)
                scored.append(item)
            scored.sort(key=lambda x: (-x["score"], x["last_used_at"]))

            selected: list[dict[str, Any]] = []
            used_chars = 0
            for item in scored:
                if len(selected) >= max(1, min(int(k), 12)):
                    break
                if any(_similarity(item["content"], prev["content"]) >= NEAR_DUPLICATE for prev in selected):
                    continue
                if used_chars + len(item["content"]) > max(120, int(max_chars)):
                    continue
                selected.append(item)
                used_chars += len(item["content"])

            if selected:
                with conn:
                    conn.executemany(
                        "UPDATE user_memories SET hits=hits+1, last_used_at=? WHERE id=?",
                        [(iso(), item["id"]) for item in selected],
                    )
                for item in selected:
                    item["hits"] = int(item["hits"]) + 1
            return selected
        finally:
            conn.close()

    def snapshot(self, memory_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_memories WHERE id=?", (memory_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _restore_snapshot(self, snapshot: dict[str, Any]) -> None:
        columns = [
            "id", "user_id", "kind", "content", "subject", "predicate",
            "object_text", "importance", "confidence", "hits",
            "source_conversation_id", "source_message_id", "created_at",
            "updated_at", "last_used_at", "valid_from", "scope", "scope_id",
            "agent_id", "source_uri", "evidence_snippet", "created_by", "status",
            "approved_by", "approved_at", "authority", "valid_to", "superseded_by",
        ]
        values = [snapshot.get(column) for column in columns]
        conn = self._conn()
        try:
            with conn:
                conn.execute("DELETE FROM user_memories WHERE id=?", (snapshot.get("id"),))
                placeholders = ",".join("?" for _ in columns)
                conn.execute(
                    f"INSERT INTO user_memories({','.join(columns)}) VALUES({placeholders})",
                    tuple(values),
                )
        finally:
            conn.close()

    def approve(
        self, memory_id: str, *, approver: str, user: dict[str, Any]
    ) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_memories WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            scope = str(row["scope"] or "personal")
            role = str(user.get("role") or "member")
            allowed = role == "admin" or (
                role == "manager"
                and scope == "department"
                and row["scope_id"] == str(user.get("department") or "")
            )
            if not allowed:
                return None
            authority = 2 if role == "admin" else 1
            now = iso()
            with conn:
                conflicts = conn.execute(
                    "SELECT * FROM user_memories WHERE scope=? AND scope_id=?"
                    " AND subject=? AND predicate=? AND valid_to IS NULL"
                    " AND status='active' AND id<>?",
                    (scope, row["scope_id"], row["subject"], row["predicate"], memory_id),
                ).fetchall() if row["subject"] and row["predicate"] else []
                for conflict in conflicts:
                    if int(conflict["authority"] or 0) > authority:
                        raise MemoryConflict(
                            f"现有记忆权威性更高（{conflict['authority']} > {authority}）"
                        )
                if self.semantic_judge is not None:
                    others = conn.execute(
                        "SELECT content FROM user_memories WHERE scope=? AND scope_id=?"
                        " AND valid_to IS NULL AND status='active' AND id<>? LIMIT 10",
                        (scope, row["scope_id"], memory_id),
                    ).fetchall()
                    for other in others:
                        relation = self._semantic_relation(
                            other["content"], row["content"]
                        )
                        if relation == "conflict":
                            raise MemoryConflict("与已有共享记忆语义冲突")
                for conflict in conflicts:
                    conn.execute(
                        "UPDATE user_memories SET valid_to=?, superseded_by=?, updated_at=?"
                        " WHERE id=?",
                        (now, memory_id, now, conflict["id"]),
                    )
                conn.execute(
                    "UPDATE user_memories SET status='active', approved_by=?,"
                    " approved_at=?, authority=?, updated_at=? WHERE id=?",
                    (approver, now, authority, now, memory_id),
                )
            return self.snapshot(memory_id)
        finally:
            conn.close()

    def reject(
        self, memory_id: str, *, approver: str, user: dict[str, Any], note: str = ""
    ) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_memories WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            scope = str(row["scope"] or "personal")
            role = str(user.get("role") or "member")
            allowed = role == "admin" or (
                role == "manager"
                and scope == "department"
                and row["scope_id"] == str(user.get("department") or "")
            )
            if not allowed:
                return None
            now = iso()
            with conn:
                conn.execute(
                    "UPDATE user_memories SET status='rejected', approved_by=?,"
                    " approved_at=?, authority=0, updated_at=? WHERE id=?",
                    (approver, now, now, memory_id),
                )
            return self.snapshot(memory_id)
        finally:
            conn.close()

    def pending(self, *, user: dict[str, Any]) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            role = str(user.get("role") or "member")
            if role == "admin":
                rows = conn.execute(
                    "SELECT * FROM user_memories WHERE status='pending'"
                    " AND valid_to IS NULL ORDER BY created_at DESC"
                ).fetchall()
            elif role == "manager":
                rows = conn.execute(
                    "SELECT * FROM user_memories WHERE status='pending'"
                    " AND valid_to IS NULL AND scope='department' AND scope_id=?"
                    " ORDER BY created_at DESC",
                    (str(user.get("department") or ""),),
                ).fetchall()
            else:
                return []
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def undo_event(self, metadata: dict[str, Any]) -> dict[str, Any] | None:
        """Undo a memory audit event using its stored snapshots."""
        snapshot = metadata.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("id"):
            return None
        action = str(metadata.get("op") or "")
        if action == "delete":
            self._restore_snapshot(snapshot)
            return {"action": "restore", "memory_id": snapshot["id"]}
        if action == "create":
            conn = self._conn()
            try:
                with conn:
                    conn.execute("DELETE FROM user_memories WHERE id=?", (snapshot["id"],))
            finally:
                conn.close()
            for old in metadata.get("superseded_snapshots") or []:
                if isinstance(old, dict) and old.get("id"):
                    self._restore_snapshot(old)
            return {"action": "delete", "memory_id": snapshot["id"]}
        if action in {"approve", "reject"} and metadata.get("previous_status"):
            conn = self._conn()
            try:
                with conn:
                    conn.execute(
                        "UPDATE user_memories SET status=?, approved_by='', approved_at='',"
                        " updated_at=? WHERE id=?",
                        (metadata["previous_status"], iso(), snapshot["id"]),
                    )
            finally:
                conn.close()
            return {"action": "status", "memory_id": snapshot["id"], "status": metadata["previous_status"]}
        return None

    def team(
        self,
        *,
        user: dict[str, Any],
        scope: str = "",
        status: str = "",
        agent_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        role = str(user.get("role") or "member")
        if role not in {"admin", "manager"}:
            return []
        clauses = ["scope IN ('department','org')", "valid_to IS NULL"]
        params: list[Any] = []
        if scope in {"department", "org"}:
            clauses.append("scope=?")
            params.append(scope)
        if status in {"active", "pending", "rejected"}:
            clauses.append("status=?")
            params.append(status)
        if agent_id:
            clauses.append("agent_id=?")
            params.append(agent_id)
        if role != "admin":
            clauses.append("(scope='org' OR scope_id=?)")
            params.append(str(user.get("department") or ""))
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM user_memories WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def consolidate_all(self, *, expire_days: int = 180) -> dict[str, Any]:
        conn = self._conn()
        try:
            users = [
                row["user_id"]
                for row in conn.execute(
                    "SELECT DISTINCT user_id FROM user_memories WHERE valid_to IS NULL"
                    " AND scope='personal'"
                ).fetchall()
            ]
        finally:
            conn.close()
        merged = 0
        expired = 0
        for user_id in users:
            result = self.consolidate(user_id, expire_days=expire_days)
            merged += int(result.get("merged") or 0)
            expired += int(result.get("expired") or 0)
        return {"users": len(users), "merged": merged, "expired": expired}

    def consolidate(self, user_id: str, *, expire_days: int = 0) -> dict[str, Any]:
        """Merge near-duplicate active memories and optionally expire stale low-value ones."""
        conn = self._conn()
        merged = 0
        expired = 0
        now = iso()
        try:
            with conn:
                rows = conn.execute(
                    "SELECT * FROM user_memories WHERE user_id=? AND valid_to IS NULL",
                    (user_id,),
                ).fetchall()
                rows = sorted(
                    rows,
                    key=lambda row: (
                        float(row["confidence"]) * 0.6 + float(row["importance"]) * 0.4,
                        row["updated_at"],
                    ),
                    reverse=True,
                )
                discarded: set[str] = set()
                for index, row in enumerate(rows):
                    if row["id"] in discarded:
                        continue
                    for other in rows[index + 1:]:
                        if other["id"] in discarded:
                            continue
                        same_slot = (
                            row["subject"]
                            and row["subject"] == other["subject"]
                            and row["predicate"] == other["predicate"]
                        )
                        similarity = _similarity(row["content"], other["content"])
                        semantic = (
                            not same_slot
                            and row["kind"] == other["kind"]
                            and similarity >= SEMANTIC_CANDIDATE
                            and similarity < NEAR_DUPLICATE
                            and self._semantically_same(row["content"], other["content"], row["kind"])
                        )
                        if not same_slot and similarity < NEAR_DUPLICATE and not semantic:
                            continue
                        conn.execute(
                            "UPDATE user_memories SET valid_to=?, superseded_by=?, updated_at=?"
                            " WHERE id=?",
                            (now, row["id"], now, other["id"]),
                        )
                        discarded.add(other["id"])
                        merged += 1

                if expire_days > 0:
                    cutoff = datetime.now(timezone.utc).timestamp() - expire_days * 86400
                    for row in rows:
                        if row["id"] in discarded:
                            continue
                        try:
                            created = datetime.fromisoformat(row["created_at"]).timestamp()
                        except (TypeError, ValueError):
                            continue
                        if created < cutoff and float(row["importance"]) < 0.3:
                            conn.execute(
                                "UPDATE user_memories SET valid_to=?, updated_at=? WHERE id=?",
                                (now, now, row["id"]),
                            )
                            expired += 1
        finally:
            conn.close()
        return {"merged": merged, "expired": expired, "stats": self.stats(user_id)}

    def stats(self, user_id: str, *, user: dict[str, Any] | None = None) -> dict[str, Any]:
        conn = self._conn()
        try:
            visible, params = self._visible_clause(user_id, user)
            active = conn.execute(
                "SELECT COUNT(*) AS c FROM user_memories WHERE " + visible
                + " AND valid_to IS NULL AND status='active'",
                tuple(params),
            ).fetchone()["c"]
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM user_memories WHERE " + visible,
                tuple(params),
            ).fetchone()["c"]
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM user_memories WHERE " + visible
                + " AND valid_to IS NULL AND status='pending'",
                tuple(params),
            ).fetchone()["c"]
            kinds = {
                row["kind"]: row["c"]
                for row in conn.execute(
                    "SELECT kind, COUNT(*) AS c FROM user_memories"
                    " WHERE " + visible + " AND valid_to IS NULL AND status='active'"
                    " GROUP BY kind",
                    tuple(params),
                ).fetchall()
            }
            avg = conn.execute(
                "SELECT AVG(confidence) AS c FROM user_memories"
                " WHERE " + visible + " AND valid_to IS NULL AND status='active'",
                tuple(params),
            ).fetchone()["c"]
            return {
                "active": int(active),
                "pending": int(pending),
                "superseded": int(total - active),
                "kinds": kinds,
                "avg_confidence": round(float(avg or 0), 3),
            }
        finally:
            conn.close()

    def list(
        self,
        user_id: str,
        *,
        limit: int = 50,
        user: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            visible, params = self._visible_clause(user_id, user)
            rows = conn.execute(
                "SELECT * FROM user_memories WHERE " + visible
                + " AND valid_to IS NULL AND status IN ('active','pending')"
                " ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 200))),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def forget(
        self,
        user_id: str,
        memory_id: str,
        *,
        user: dict[str, Any] | None = None,
    ) -> bool:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_memories WHERE id=?", (memory_id,)
            ).fetchone()
            if row is None:
                return False
            scope = str(row["scope"] or "personal")
            allowed = row["user_id"] == user_id and scope == "personal"
            if user:
                role = str(user.get("role") or "member")
                if scope == "org":
                    allowed = role == "admin"
                elif scope == "department":
                    allowed = role == "admin" or (
                        role == "manager"
                        and row["scope_id"] == str(user.get("department") or "")
                    )
            if not allowed:
                return False
            with conn:
                cur = conn.execute(
                    "DELETE FROM user_memories WHERE id=?", (memory_id,)
                )
            return cur.rowcount > 0
        finally:
            conn.close()
