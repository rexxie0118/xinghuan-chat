"""Data-access layer. Every conversation query is scoped by user_id."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .util import conversation_title, dump_json, iso, load_json, new_id, utcnow


# ------------------------------------------------------------------ users


def user_public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"],
        "department": row["department"] if "department" in row.keys() else "产品部",
        "role": row["role"] if "role" in row.keys() else "member",
        "created_at": row["created_at"],
    }


def create_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    display_name: str,
    password_hash: str,
    department: str = "产品部",
    role: str = "member",
) -> dict[str, Any]:
    user_id = new_id("usr")
    created = iso()
    with conn:
        conn.execute(
            "INSERT INTO users(id, username, display_name, password_hash, department, role, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (user_id, username, display_name, password_hash, department, role, created),
        )
    return {
        "id": user_id,
        "username": username,
        "display_name": display_name,
        "department": department,
        "role": role,
        "created_at": created,
    }


def find_user_by_username(
    conn: sqlite3.Connection, username: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ).fetchone()


def set_user_department(conn: sqlite3.Connection, user_id: str, department: str) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET department = ? WHERE id = ?", (department, user_id)
        )


def update_user_role_department(
    conn: sqlite3.Connection, user_id: str, *, role: str, department: str
) -> None:
    with conn:
        conn.execute(
            "UPDATE users SET role=?, department=? WHERE id=?",
            (role, department, user_id),
        )


def get_user(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def count_users(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"])


# --------------------------------------------------------------- sessions


def save_session(
    conn: sqlite3.Connection, *, user_id: str, token_hash: str, expires: str
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at)"
            " VALUES(?,?,?,?)",
            (token_hash, user_id, iso(), expires),
        )


def session_user(conn: sqlite3.Connection, token_hash: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    try:
        expired = datetime.fromisoformat(row["expires_at"]) <= utcnow()
    except ValueError:
        expired = True
    if expired:
        delete_session(conn, token_hash)
        return None
    return user_public(row)


def delete_session(conn: sqlite3.Connection, token_hash: str) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def purge_expired_sessions(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso(),))


# ---------------------------------------------------------- conversations


def _conversation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "mode": row["mode"],
        "kb_id": row["kb_id"] if "kb_id" in row.keys() else "product",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "message_count": row["message_count"] if "message_count" in row.keys() else 0,
        "last_message": row["last_message"] if "last_message" in row.keys() else None,
    }


def create_conversation(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    mode: str = "agent",
    kb_id: str = "product",
) -> dict[str, Any]:
    conv_id = new_id("conv")
    ts = iso()
    with conn:
        conn.execute(
            "INSERT INTO conversations(id, user_id, title, mode, kb_id, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (conv_id, user_id, "新的对话", mode, kb_id, ts, ts),
        )
    row = conn.execute(
        "SELECT c.*, 0 AS message_count, NULL AS last_message"
        " FROM conversations c WHERE c.id = ?",
        (conv_id,),
    ).fetchone()
    return _conversation(row)


def list_conversations(
    conn: sqlite3.Connection, user_id: str, *, kb_id: str | None = None
) -> list[dict[str, Any]]:
    where = "c.user_id = ?"
    params: list[Any] = [user_id]
    if kb_id:
        where += " AND c.kb_id = ?"
        params.append(kb_id)
    rows = conn.execute(
        f"""
        SELECT c.*,
               (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
               (SELECT m.content FROM messages m WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC LIMIT 1) AS last_message
        FROM conversations c
        WHERE {where}
        ORDER BY c.updated_at DESC
        """,
        params,
    ).fetchall()
    out = []
    for row in rows:
        item = _conversation(row)
        if item["last_message"]:
            item["last_message"] = " ".join(item["last_message"].split())[:90]
        out.append(item)
    return out


def get_conversation(
    conn: sqlite3.Connection, *, conversation_id: str, user_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
               (SELECT m.content FROM messages m WHERE m.conversation_id = c.id
                ORDER BY m.created_at DESC LIMIT 1) AS last_message
        FROM conversations c
        WHERE c.id = ? AND c.user_id = ?
        """,
        (conversation_id, user_id),
    ).fetchone()
    return _conversation(row) if row else None


def rename_conversation(
    conn: sqlite3.Connection, *, conversation_id: str, user_id: str, title: str
) -> bool:
    with conn:
        cur = conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (title, iso(), conversation_id, user_id),
        )
    return cur.rowcount > 0


def delete_conversation(
    conn: sqlite3.Connection, *, conversation_id: str, user_id: str
) -> bool:
    with conn:
        cur = conn.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
    return cur.rowcount > 0


def touch_conversation(
    conn: sqlite3.Connection, *, conversation_id: str, user_id: str
) -> None:
    with conn:
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ? AND user_id = ?",
            (iso(), conversation_id, user_id),
        )


def auto_title_if_needed(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    user_id: str,
    first_user_text: str,
) -> str | None:
    row = conn.execute(
        "SELECT title, (SELECT COUNT(*) FROM messages WHERE conversation_id = ?) AS n"
        " FROM conversations WHERE id = ? AND user_id = ?",
        (conversation_id, conversation_id, user_id),
    ).fetchone()
    if row is None or row["n"] > 1 or row["title"] != "新的对话":
        return None
    title = conversation_title(first_user_text)
    with conn:
        conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ? AND user_id = ?",
            (title, conversation_id, user_id),
        )
    return title


# --------------------------------------------------------------- messages


def _message(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "role": row["role"],
        "content": row["content"],
        "citations": load_json(row["citations_json"], []),
        "meta": load_json(row["meta_json"], {}),
        "created_at": row["created_at"],
    }


def add_message(
    conn: sqlite3.Connection,
    *,
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    citations: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message_id = new_id("msg")
    created = iso()
    with conn:
        conn.execute(
            "INSERT INTO messages(id, conversation_id, user_id, role, content,"
            " citations_json, meta_json, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                message_id,
                conversation_id,
                user_id,
                role,
                content,
                dump_json(citations or []),
                dump_json(meta or {}),
                created,
            ),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ? AND user_id = ?",
            (created, conversation_id, user_id),
        )
    return {
        "id": message_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "citations": citations or [],
        "meta": meta or {},
        "created_at": created,
    }


def list_messages(
    conn: sqlite3.Connection, *, conversation_id: str, user_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND user_id = ?"
        " ORDER BY created_at ASC",
        (conversation_id, user_id),
    ).fetchall()
    return [_message(row) for row in rows]


def conversation_context(
    conn: sqlite3.Connection, *, conversation_id: str, user_id: str, limit: int = 8
) -> list[dict[str, str]]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? AND user_id = ?"
        " ORDER BY created_at DESC LIMIT ?",
        (conversation_id, user_id, limit),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
