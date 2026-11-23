"""Persistent per-user container and per-conversation workspace mapping.

The runtime manager talks to Apple Container; this module owns the durable
mapping between WebChat users/conversations and that runtime. It deliberately
does not run Agent scripts: that remains the SandboxRunner's job.
"""

from __future__ import annotations

import logging
import sqlite3
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
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE container_workspaces SET last_active_at=?"
                    " WHERE conversation_id=? AND user_id=?",
                    (iso(), conversation_id, user_id),
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
