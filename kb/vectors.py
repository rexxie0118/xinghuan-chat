"""向量存取：float32 blob + 全量矩阵加载。

MVP 选择"暴力余弦"而不是 HNSW：十万级 chunk 以内，numpy 全量点积是毫秒级，
换来的是零索引维护成本、零召回损失。超过阈值再切向量库，接口保持不变。
"""

from __future__ import annotations

import json
from typing import Sequence

import numpy as np

from .util import iso

KIND_CHUNK = "chunk"
KIND_MEMORY = "memory"
KIND_ENTITY = "entity"


def _is_pg(conn) -> bool:
    return bool(getattr(conn, "is_postgres", False))


def _conflict_target(conn) -> str:
    return "kb_id, owner_kind, owner_id" if _is_pg(conn) else "owner_kind, owner_id"


def _vector_literal(arr: np.ndarray) -> str:
    return "[" + ",".join(f"{float(value):.8g}" for value in arr) + "]"


def _parse_vector(value: object) -> np.ndarray:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return np.frombuffer(bytes(value), dtype=np.float32).copy()
    text = str(value or "").strip()
    return np.asarray(json.loads(text) if text else [], dtype=np.float32)


def put_vector(conn, owner_kind: str, owner_id: str, model: str, vec: np.ndarray) -> None:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if _is_pg(conn):
        conn.execute(
            "INSERT INTO vectors(owner_kind, owner_id, model, dim, vec, updated_at)"
            " VALUES(?,?,?,?,?::vector,?)"
            f" ON CONFLICT({_conflict_target(conn)}) DO UPDATE SET"
            " model=excluded.model, dim=excluded.dim, vec=excluded.vec, updated_at=excluded.updated_at",
            (owner_kind, owner_id, model, int(arr.size), _vector_literal(arr), iso()),
        )
        return
    conn.execute(
        "INSERT INTO vectors(owner_kind, owner_id, model, dim, vec, updated_at)"
        " VALUES(?,?,?,?,?,?)"
        f" ON CONFLICT({_conflict_target(conn)}) DO UPDATE SET"
        " model=excluded.model, dim=excluded.dim, vec=excluded.vec, updated_at=excluded.updated_at",
        (owner_kind, owner_id, model, int(arr.size), arr.tobytes(), iso()),
    )


def put_vectors(conn, owner_kind: str, model: str, ids: Sequence[str], matrix: np.ndarray) -> None:
    ts = iso()
    rows = []
    for i, owner_id in enumerate(ids):
        arr = np.asarray(matrix[i], dtype=np.float32).reshape(-1)
        values = _vector_literal(arr) if _is_pg(conn) else arr.tobytes()
        rows.append((owner_kind, owner_id, model, int(arr.size), values, ts))
    placeholder = "?::vector" if _is_pg(conn) else "?"
    conn.executemany(
        "INSERT INTO vectors(owner_kind, owner_id, model, dim, vec, updated_at)"
        f" VALUES(?,?,?,?,{placeholder},?)"
        f" ON CONFLICT({_conflict_target(conn)}) DO UPDATE SET"
        " model=excluded.model, dim=excluded.dim, vec=excluded.vec, updated_at=excluded.updated_at",
        rows,
    )


def get_vector(conn, owner_kind: str, owner_id: str) -> np.ndarray | None:
    row = conn.execute(
        "SELECT dim, vec FROM vectors WHERE owner_kind=? AND owner_id=?",
        (owner_kind, owner_id),
    ).fetchone()
    if row is None:
        return None
    return _parse_vector(row["vec"])


def delete_vector(conn, owner_kind: str, owner_id: str) -> None:
    conn.execute(
        "DELETE FROM vectors WHERE owner_kind=? AND owner_id=?", (owner_kind, owner_id)
    )


def load_matrix(
    conn, owner_kind: str, *, model: str | None = None
) -> tuple[list[str], np.ndarray, int]:
    """加载某一类向量为 (ids, matrix, dim)。模型不匹配的行会被跳过。"""
    sql = "SELECT owner_id, model, dim, vec FROM vectors WHERE owner_kind=?"
    params: list = [owner_kind]
    if model:
        sql += " AND model=?"
        params.append(model)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return [], np.zeros((0, 0), dtype=np.float32), 0
    dim = int(rows[0]["dim"])
    ids: list[str] = []
    vecs: list[np.ndarray] = []
    for r in rows:
        if int(r["dim"]) != dim:
            continue  # 换过 embedding 模型的历史向量，等 reindex 修复
        ids.append(r["owner_id"])
        vecs.append(_parse_vector(r["vec"]))
    return ids, np.vstack(vecs) if vecs else np.zeros((0, dim), dtype=np.float32), dim


def vector_models(conn) -> dict[str, int]:
    rows = conn.execute(
        "SELECT model, COUNT(*) c FROM vectors GROUP BY model ORDER BY c DESC"
    ).fetchall()
    return {r["model"]: int(r["c"]) for r in rows}
