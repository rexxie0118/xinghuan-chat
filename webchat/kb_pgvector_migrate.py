"""Convert the KB vectors column from BYTEA to pgvector vector(256).

This is a one-time schema migration for KB databases created before pgvector
was wired in. Run it after stopping Web/Worker:

    WEBCHAT_KB_DB_URL=... python -m webchat.kb_pgvector_migrate --data-dir webchat/data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from kb.db import connect
from kb.vectors import _vector_literal
from webchat.kb_pg_migrate import _discover


def _column_type(conn, table: str, column: str) -> str:
    row = conn.execute(
        "SELECT udt_name FROM information_schema.columns"
        " WHERE table_name=? AND column_name=?",
        (table, column),
    ).fetchone()
    return str(row["udt_name"]) if row else ""


def migrate(data_dir: Path, *, verify: bool = False) -> dict[str, Any]:
    files = _discover(data_dir)
    if not files:
        raise RuntimeError(f"没有找到 kb_*.sqlite3：{data_dir}")
    first = next(iter(files))
    admin = connect(Path("unused.sqlite3"), kb_id=first)
    try:
        col_type = _column_type(admin, "vectors", "vec")
        if col_type != "vector":
            admin.execute(
                "ALTER TABLE vectors ADD COLUMN IF NOT EXISTS vec_vector vector(256)"
            )
        converted = 0
        for kb_id in files:
            conn = connect(Path("unused.sqlite3"), kb_id=kb_id)
            try:
                if col_type != "vector":
                    rows = conn.execute(
                        "SELECT owner_kind, owner_id, vec FROM vectors"
                    ).fetchall()
                    for row in rows:
                        conn.execute(
                            "UPDATE vectors SET vec_vector=?::vector"
                            " WHERE owner_kind=? AND owner_id=?",
                            (
                                _vector_literal(np.frombuffer(bytes(row["vec"]), dtype=np.float32)),
                                row["owner_kind"],
                                row["owner_id"],
                            ),
                        )
                        converted += 1
                if verify:
                    count = conn.execute(
                        "SELECT COUNT(*) AS c FROM vectors WHERE vec IS NOT NULL"
                    ).fetchone()["c"]
                    print(f"KB {kb_id}: vectors={count}")
            finally:
                conn.close()
        if col_type != "vector":
            admin.execute("ALTER TABLE vectors DROP COLUMN vec")
            admin.execute("ALTER TABLE vectors RENAME COLUMN vec_vector TO vec")
        admin.execute(
            "CREATE INDEX IF NOT EXISTS idx_vectors_vec_hnsw"
            " ON vectors USING hnsw (vec vector_cosine_ops)"
        )
        admin.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm"
            " ON chunks USING gin (text gin_trgm_ops)"
        )
        return {"from": col_type or "bytea", "to": "vector", "converted": converted}
    finally:
        admin.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert KB vectors BYTEA -> pgvector")
    parser.add_argument("--data-dir", default="webchat/data")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = migrate(Path(args.data_dir), verify=args.verify)
    except Exception as exc:  # noqa: BLE001
        print(f"转换失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
