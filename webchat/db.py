"""SQLite persistence for accounts, sessions, conversations and messages."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE,
        display_name  TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        department    TEXT NOT NULL DEFAULT '产品部',
        role          TEXT NOT NULL DEFAULT 'member',
        created_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id         TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title      TEXT NOT NULL,
        mode       TEXT NOT NULL DEFAULT 'agent',
        kb_id      TEXT NOT NULL DEFAULT 'product',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id              TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role            TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
        content         TEXT NOT NULL,
        citations_json  TEXT NOT NULL DEFAULT '[]',
        meta_json       TEXT NOT NULL DEFAULT '{}',
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS agents (
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        markdown    TEXT NOT NULL,
        entrypoint  TEXT NOT NULL DEFAULT '',
        runtime     TEXT NOT NULL DEFAULT 'python',
        timeout     REAL NOT NULL DEFAULT 10,
        files_json  TEXT NOT NULL DEFAULT '[]',
        created_at  TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'agent',
        source_url  TEXT NOT NULL DEFAULT '',
        source_ref  TEXT NOT NULL DEFAULT '',
        source_path TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
        id             TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        agent_id       TEXT NOT NULL DEFAULT '',
        agent_name     TEXT NOT NULL DEFAULT '',
        created_at     TEXT NOT NULL,
        execution_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_user ON agent_runs(user_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS user_runtimes (
        user_id        TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        runtime_key    TEXT NOT NULL UNIQUE,
        container_name TEXT NOT NULL,
        volume_name    TEXT NOT NULL,
        image          TEXT NOT NULL,
        state          TEXT NOT NULL DEFAULT 'missing',
        created_at     TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS container_workspaces (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        runtime_key     TEXT NOT NULL,
        workspace_path  TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        last_active_at  TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_container_workspaces_user ON container_workspaces(user_id, last_active_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS container_checkpoints (
        conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
        user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        runtime_key     TEXT NOT NULL,
        workspace_path  TEXT NOT NULL,
        last_message_id TEXT NOT NULL DEFAULT '',
        last_run_id     TEXT NOT NULL DEFAULT '',
        state_json      TEXT NOT NULL DEFAULT '{}',
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_container_checkpoints_user ON container_checkpoints(user_id, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS user_memories (
        id                     TEXT PRIMARY KEY,
        user_id                TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind                   TEXT NOT NULL,
        content                TEXT NOT NULL,
        subject                TEXT NOT NULL DEFAULT '',
        predicate              TEXT NOT NULL DEFAULT '',
        object_text            TEXT NOT NULL DEFAULT '',
        importance             REAL NOT NULL DEFAULT 0.5,
        confidence             REAL NOT NULL DEFAULT 0.7,
        hits                   INTEGER NOT NULL DEFAULT 0,
        source_conversation_id TEXT NOT NULL DEFAULT '',
        source_message_id      TEXT NOT NULL DEFAULT '',
        created_at             TEXT NOT NULL,
        valid_from             TEXT NOT NULL DEFAULT '',
        updated_at             TEXT NOT NULL,
        last_used_at           TEXT NOT NULL,
        valid_to               TEXT,
        superseded_by          TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_memories_user ON user_memories(user_id, valid_to, last_used_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS connectors (
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name        TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        spec_json   TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'draft',
        source      TEXT NOT NULL DEFAULT 'manual',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        approved_at TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_connectors_user ON connectors(user_id, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id              TEXT PRIMARY KEY,
        request_id      TEXT NOT NULL DEFAULT '',
        user_id         TEXT NOT NULL DEFAULT '',
        conversation_id TEXT NOT NULL DEFAULT '',
        run_id          TEXT NOT NULL DEFAULT '',
        event_type      TEXT NOT NULL,
        component       TEXT NOT NULL DEFAULT '',
        action          TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'ok',
        duration_ms     INTEGER NOT NULL DEFAULT 0,
        failure_class   TEXT NOT NULL DEFAULT '',
        metadata_json   TEXT NOT NULL DEFAULT '{}',
        created_at      TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_events(request_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_user_created ON audit_events(user_id, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS department_agents (
        id          TEXT PRIMARY KEY,
        department  TEXT NOT NULL,
        name        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        markdown    TEXT NOT NULL,
        entrypoint  TEXT NOT NULL DEFAULT '',
        runtime     TEXT NOT NULL DEFAULT 'python',
        timeout     REAL NOT NULL DEFAULT 10,
        files_json  TEXT NOT NULL DEFAULT '[]',
        created_at  TEXT NOT NULL,
        kind        TEXT NOT NULL DEFAULT 'skill',
        enabled     INTEGER NOT NULL DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_department_agents_dept ON department_agents(department, enabled)",
]


DB_URL_ENV = "WEBCHAT_DB_URL"


def is_integrity_error(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    try:
        import psycopg
        return isinstance(exc, psycopg.errors.UniqueViolation)
    except Exception:  # pragma: no cover - optional driver
        return False


def _pg_sql(sql: str) -> str:
    # The repository intentionally uses SQLite-style qmark placeholders; this
    # translation keeps both backends on one SQL surface.
    return sql.replace("?", "%s")


class _PgCursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", -1))


class PgConnection:
    """Small DB-API facade over psycopg with dict rows.

    Code uses only ``execute`` / ``executemany`` / ``with conn`` / row mapping,
    so this wrapper keeps the existing repository unchanged.
    """

    is_postgres = True

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - optional infra
            raise RuntimeError(
                "使用 WEBCHAT_DB_URL=postgresql:// 需要安装 psycopg[binary]"
            ) from exc
        self._raw = psycopg.connect(dsn, row_factory=dict_row)

    def execute(self, sql: str, params: Any = ()) -> _PgCursor:
        return _PgCursor(self._raw.execute(_pg_sql(sql), params))

    def executemany(self, sql: str, seq: Any) -> _PgCursor:
        return _PgCursor(self._raw.executemany(_pg_sql(sql), seq))

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._raw.__exit__(exc_type, exc, tb)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


def database_url() -> str:
    return (os.getenv(DB_URL_ENV) or "").strip()


def connect(path: Path | str) -> Any:
    dsn = database_url()
    if dsn.startswith(("postgres://", "postgresql://")):
        return PgConnection(dsn)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_columns(conn: Any, table: str, columns: dict[str, str]) -> None:
    if getattr(conn, "is_postgres", False):
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=?",
            (table,),
        ).fetchall()
        existing = {row["column_name"] for row in rows}
    else:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db(path: Path | str) -> None:
    conn = connect(path)
    try:
        with conn:
            for stmt in SCHEMA:
                conn.execute(stmt)
            # Lightweight migrations for databases created before department KBs.
            _ensure_columns(
                conn,
                "users",
                {
                    "department": "TEXT NOT NULL DEFAULT '产品部'",
                    "role": "TEXT NOT NULL DEFAULT 'member'",
                },
            )
            _ensure_columns(
                conn,
                "conversations",
                {"kb_id": "TEXT NOT NULL DEFAULT 'product'"},
            )
            _ensure_columns(
                conn,
                "user_memories",
                {
                    "valid_from": "TEXT NOT NULL DEFAULT ''",
                    "scope": "TEXT NOT NULL DEFAULT 'personal'",
                    "scope_id": "TEXT NOT NULL DEFAULT ''",
                    "agent_id": "TEXT NOT NULL DEFAULT ''",
                    "source_uri": "TEXT NOT NULL DEFAULT ''",
                    "evidence_snippet": "TEXT NOT NULL DEFAULT ''",
                    "created_by": "TEXT NOT NULL DEFAULT ''",
                    "status": "TEXT NOT NULL DEFAULT 'active'",
                    "approved_by": "TEXT NOT NULL DEFAULT ''",
                    "approved_at": "TEXT NOT NULL DEFAULT ''",
                    "authority": "INTEGER NOT NULL DEFAULT 0",
                },
            )
            _ensure_columns(
                conn,
                "agents",
                {
                    "kind": "TEXT NOT NULL DEFAULT 'agent'",
                    "source_url": "TEXT NOT NULL DEFAULT ''",
                    "source_ref": "TEXT NOT NULL DEFAULT ''",
                    "source_path": "TEXT NOT NULL DEFAULT ''",
                },
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_user_kb"
                " ON conversations(user_id, kb_id, updated_at DESC)"
            )
    finally:
        conn.close()
