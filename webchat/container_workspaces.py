"""Persistent per-user container and per-conversation workspace mapping.

The runtime manager talks to Apple Container; this module owns the durable
mapping between WebChat users/conversations and that runtime. It deliberately
does not run Agent scripts: that remains the SandboxRunner's job.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import connect
from .sandbox.apple_runtime import AppleContainerRuntime, ContainerRuntimeError, RuntimeInfo
from .util import iso

LOG = logging.getLogger("webchat.container_workspaces")
WORKSPACE_ROOT = "/workspace/sessions"


def runtime_key(user_id: str) -> str:
    return f"user:{user_id}"


def workspace_path(conversation_id: str) -> str:
    safe = "".join(ch for ch in conversation_id if ch.isalnum() or ch in "-_")[:80]
    return f"{WORKSPACE_ROOT}/{safe or 'default'}"


class ContainerWorkspaceService:
    def __init__(self, settings: Any, runtime: AppleContainerRuntime) -> None:
        self.settings = settings
        self.runtime = runtime
        self.db_path = Path(settings.db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def get(self, user_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_runtimes WHERE user_id=?", (user_id,)
            ).fetchone()
            if row is None:
                return None
            workspaces = conn.execute(
                "SELECT * FROM container_workspaces WHERE user_id=?"
                " ORDER BY last_active_at DESC",
                (user_id,),
            ).fetchall()
            return {"runtime": dict(row), "workspaces": [dict(w) for w in workspaces]}
        finally:
            conn.close()

    def ensure_user(self, user_id: str) -> dict[str, Any]:
        key = runtime_key(user_id)
        info = self.runtime.ensure(key)
        now = iso()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO user_runtimes(user_id, runtime_key, container_name,
                        volume_name, image, state, created_at, last_active_at, updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        container_name=excluded.container_name,
                        volume_name=excluded.volume_name,
                        image=excluded.image,
                        state=excluded.state,
                        last_active_at=excluded.last_active_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        user_id,
                        key,
                        info.name,
                        info.volume,
                        info.image,
                        info.state,
                        now,
                        now,
                        now,
                    ),
                )
        finally:
            conn.close()
        return {
            "runtime_key": key,
            "container_name": info.name,
            "volume_name": info.volume,
            "image": info.image,
            "state": info.state,
        }

    def ensure_workspace(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        runtime = self.ensure_user(user_id)
        path = workspace_path(conversation_id)
        self.runtime.exec(runtime_key(user_id), ["mkdir", "-p", path], timeout=20)
        now = iso()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO container_workspaces(conversation_id, user_id,
                        runtime_key, workspace_path, created_at, last_active_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        runtime_key=excluded.runtime_key,
                        workspace_path=excluded.workspace_path,
                        last_active_at=excluded.last_active_at
                    """,
                    (conversation_id, user_id, runtime_key(user_id), path, now, now),
                )
        finally:
            conn.close()
        return {**runtime, "conversation_id": conversation_id, "workspace_path": path}

    def touch_workspace(self, user_id: str, conversation_id: str) -> None:
        now = iso()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE user_runtimes SET last_active_at=?, updated_at=?"
                    " WHERE user_id=?",
                    (now, now, user_id),
                )
                conn.execute(
                    "UPDATE container_workspaces SET last_active_at=?"
                    " WHERE conversation_id=? AND user_id=?",
                    (now, conversation_id, user_id),
                )
        finally:
            conn.close()

    def save_checkpoint(
        self,
        user_id: str,
        conversation_id: str,
        *,
        last_message_id: str,
        last_run_id: str = "",
        state: dict[str, Any] | None = None,
    ) -> None:
        mapping = self.get(user_id)
        workspace = None
        if mapping:
            for item in mapping["workspaces"]:
                if item["conversation_id"] == conversation_id:
                    workspace = item
                    break
        runtime = (mapping or {}).get("runtime") or {}
        path = (workspace or {}).get("workspace_path") or workspace_path(conversation_id)
        key = runtime.get("runtime_key") or runtime_key(user_id)
        now = iso()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO container_checkpoints(conversation_id, user_id,
                        runtime_key, workspace_path, last_message_id, last_run_id,
                        state_json, updated_at)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        runtime_key=excluded.runtime_key,
                        workspace_path=excluded.workspace_path,
                        last_message_id=excluded.last_message_id,
                        last_run_id=excluded.last_run_id,
                        state_json=excluded.state_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        conversation_id,
                        user_id,
                        key,
                        path,
                        last_message_id,
                        last_run_id,
                        json.dumps(state or {}, ensure_ascii=False),
                        now,
                    ),
                )
        finally:
            conn.close()

    def get_checkpoint(self, user_id: str, conversation_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM container_checkpoints WHERE conversation_id=? AND user_id=?",
                (conversation_id, user_id),
            ).fetchone()
            if row is None:
                return None
            data = dict(row)
            try:
                data["state"] = json.loads(data.pop("state_json") or "{}")
            except ValueError:
                data["state"] = {}
            return data
        finally:
            conn.close()

    def restore(self, user_id: str, conversation_id: str) -> dict[str, Any]:
        mapping = self.ensure_workspace(user_id, conversation_id)
        checkpoint = self.get_checkpoint(user_id, conversation_id)
        return {"mapping": mapping, "checkpoint": checkpoint}

    def reap_idle(
        self,
        *,
        stop_after_seconds: int,
        delete_after_seconds: int,
        purge_after_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Stop/delete/purge idle user runtimes.

        Container deletion keeps the named volume (workspace). Purge removes
        the volume and the corresponding mapping/checkpoint rows. The threshold
        order is purge > delete > stop.
        """
        current = now or datetime.now(timezone.utc)
        conn = self._conn()
        try:
            rows = conn.execute("SELECT * FROM user_runtimes").fetchall()
        finally:
            conn.close()
        summary = {"stopped": 0, "deleted": 0, "purged": 0, "errors": 0}
        for row in rows:
            user_id = row["user_id"]
            key = row["runtime_key"]
            state = row["state"]
            try:
                last = datetime.fromisoformat(row["last_active_at"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                age = (current - last).total_seconds()
            except (TypeError, ValueError):
                continue
            if age >= purge_after_seconds:
                try:
                    self.runtime.delete(key, delete_volume=True)
                    db = self._conn()
                    try:
                        with db:
                            db.execute("DELETE FROM container_checkpoints WHERE user_id=?", (user_id,))
                            db.execute("DELETE FROM container_workspaces WHERE user_id=?", (user_id,))
                            db.execute("DELETE FROM user_runtimes WHERE user_id=?", (user_id,))
                    finally:
                        db.close()
                    summary["purged"] += 1
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("runtime purge failed for %s: %s", user_id, exc)
                    summary["errors"] += 1
                continue
            if age >= delete_after_seconds and state in {"running", "stopped"}:
                try:
                    self.runtime.delete(key, delete_volume=False)
                    self._set_runtime_state(user_id, "deleted")
                    summary["deleted"] += 1
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("runtime delete failed for %s: %s", user_id, exc)
                    summary["errors"] += 1
                continue
            if age >= stop_after_seconds and state == "running":
                try:
                    self.runtime.stop(key)
                    self._set_runtime_state(user_id, "stopped")
                    summary["stopped"] += 1
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("runtime stop failed for %s: %s", user_id, exc)
                    summary["errors"] += 1
        return summary

    def _set_runtime_state(self, user_id: str, state: str) -> None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE user_runtimes SET state=?, updated_at=? WHERE user_id=?",
                    (state, iso(), user_id),
                )
        finally:
            conn.close()

    def stop(self, user_id: str) -> dict[str, Any] | None:
        info = self.runtime.stop(runtime_key(user_id))
        now = iso()
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE user_runtimes SET state=?, updated_at=?, last_active_at=?"
                    " WHERE user_id=?",
                    (info.state, now, now, user_id),
                )
        finally:
            conn.close()
        return self.get(user_id)

    def delete(self, user_id: str, *, delete_volume: bool = False) -> None:
        self.runtime.delete(runtime_key(user_id), delete_volume=delete_volume)
        conn = self._conn()
        try:
            with conn:
                conn.execute("DELETE FROM container_workspaces WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM user_runtimes WHERE user_id=?", (user_id,))
        finally:
            conn.close()


class RuntimeReaper:
    """Async loop around ContainerWorkspaceService.reap_idle."""

    def __init__(
        self,
        service: ContainerWorkspaceService,
        *,
        interval_seconds: int,
        stop_after_seconds: int,
        delete_after_seconds: int,
        purge_after_seconds: int,
    ) -> None:
        self.service = service
        self.interval_seconds = max(10, int(interval_seconds))
        self.stop_after_seconds = int(stop_after_seconds)
        self.delete_after_seconds = int(delete_after_seconds)
        self.purge_after_seconds = int(purge_after_seconds)

    async def run(self, stop_event):
        import asyncio

        while not stop_event.is_set():
            try:
                summary = await asyncio.to_thread(
                    self.service.reap_idle,
                    stop_after_seconds=self.stop_after_seconds,
                    delete_after_seconds=self.delete_after_seconds,
                    purge_after_seconds=self.purge_after_seconds,
                )
                if any(summary.values()):
                    logging.getLogger("webchat.runtime_reaper").info(
                        "idle reap: %s", summary
                    )
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("webchat.runtime_reaper").warning("reap failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
