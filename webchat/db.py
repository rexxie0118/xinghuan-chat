"""SQLite persistence for accounts, sessions, conversations and messages."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id            TEXT PRIMARY KEY,
        username      TEXT NOT NULL UNIQUE,
        display_name  TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        department    TEXT NOT NULL DEFAULT '产品部',
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
]


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
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
                {"department": "TEXT NOT NULL DEFAULT '产品部'"},
            )
            _ensure_columns(
                conn,
                "conversations",
                {"kb_id": "TEXT NOT NULL DEFAULT 'product'"},
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
