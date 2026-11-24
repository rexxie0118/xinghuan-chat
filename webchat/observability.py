"""Request correlation and audit events.

Audit rows intentionally store metadata only: no prompts, tool arguments, model
outputs or secrets. Tool calls record the tool name and argument keys only.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from .db import connect
from .util import iso, new_id, utcnow

LOG = logging.getLogger("webchat.observability")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="")


def new_request_id() -> str:
    return new_id("req")


def current_request_id() -> str:
    return request_id_var.get()


@dataclass
class AuditEvent:
    event_type: str
    component: str = ""
    action: str = ""
    status: str = "ok"
    duration_ms: int = 0
    failure_class: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "component": self.component,
            "action": self.action,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "failure_class": self.failure_class,
            "metadata": self.metadata,
        }


class AuditLog:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def record(
        self,
        *,
        event_type: str,
        component: str = "",
        action: str = "",
        status: str = "ok",
        duration_ms: int = 0,
        failure_class: str = "",
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        conversation_id: str = "",
        run_id: str = "",
        request_id: str | None = None,
    ) -> None:
        try:
            event_id = new_id("evt")
            conn = self._conn()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO audit_events(id, request_id, user_id, conversation_id,
                            run_id, event_type, component, action, status, duration_ms,
                            failure_class, metadata_json, created_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            event_id,
                            request_id if request_id is not None else current_request_id(),
                            user_id if user_id is not None else user_id_var.get(),
                            conversation_id,
                            run_id,
                            event_type,
                            component,
                            action[:160],
                            status,
                            max(0, int(duration_ms)),
                            failure_class[:80],
                            json.dumps(metadata or {}, ensure_ascii=False)[:8000],
                            iso(),
                        ),
                    )
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - never break the request
            LOG.warning("audit write failed: %s", exc)

    def query(
        self,
        *,
        user_id: str | None = None,
        limit: int = 100,
        event_type: str = "",
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            clauses = []
            params: list[Any] = []
            if user_id:
                clauses.append("user_id=?")
                params.append(user_id)
            if event_type:
                clauses.append("event_type=?")
                params.append(event_type)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM audit_events{where} ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                try:
                    item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
                except ValueError:
                    item["metadata"] = {}
                out.append(item)
            return out
        finally:
            conn.close()

    def get(self, event_id: str, *, user_id: str | None = None) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            if user_id:
                row = conn.execute(
                    "SELECT * FROM audit_events WHERE id=? AND user_id=?",
                    (event_id, user_id),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM audit_events WHERE id=?", (event_id,)
                ).fetchone()
            if row is None:
                return None
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except ValueError:
                item["metadata"] = {}
            return item
        finally:
            conn.close()

    def metrics(self, *, user_id: str | None = None) -> dict[str, Any]:
        rows = self.query(user_id=user_id, limit=500)
        total = len(rows)
        errors = sum(1 for row in rows if row["status"] == "error")
        durations = [int(row["duration_ms"] or 0) for row in rows]
        by_type: dict[str, int] = {}
        by_component: dict[str, int] = {}
        failures: dict[str, int] = {}
        for row in rows:
            by_type[row["event_type"]] = by_type.get(row["event_type"], 0) + 1
            if row["component"]:
                by_component[row["component"]] = by_component.get(row["component"], 0) + 1
            if row["failure_class"]:
                failures[row["failure_class"]] = failures.get(row["failure_class"], 0) + 1
        return {
            "events": total,
            "errors": errors,
            "error_rate": round(errors / total, 3) if total else 0,
            "avg_duration_ms": round(sum(durations) / len(durations), 1) if durations else 0,
            "max_duration_ms": max(durations) if durations else 0,
            "by_type": by_type,
            "by_component": by_component,
            "failures": failures,
            "recent": rows[:20],
        }

    def prune(self, *, days: int = 30) -> int:
        cutoff = iso(utcnow() - timedelta(days=max(1, int(days))))
        conn = self._conn()
        try:
            with conn:
                cur = conn.execute("DELETE FROM audit_events WHERE created_at < ?", (cutoff,))
            return cur.rowcount
        finally:
            conn.close()
