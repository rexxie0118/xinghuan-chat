"""Migrate all SQLite kb_*.sqlite3 files into the shared PostgreSQL KB tables.

Usage:
    WEBCHAT_KB_DB_URL=postgresql://... python -m webchat.kb_pg_migrate \
      --data-dir webchat/data --verify
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

import numpy as np
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kb.db import connect, init_schema

TABLES = [
    "meta", "documents", "chunks", "chunk_fts", "vectors",
    "entity_types", "relation_types", "entities", "entity_aliases",
    "relations", "mentions", "memories", "memories_draft", "terms",
    "ingest_log",
]


@dataclass
class Result:
    table: str
    source_rows: int
    copied_rows: int
    skipped_rows: int


def _discover(data_dir: Path) -> dict[str, Path]:
    out = {}
    for path in sorted(data_dir.glob("kb_*.sqlite3")):
        kb_id = path.stem[3:]
        if kb_id:
            out[kb_id] = path
    return out


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _vector_literal(value: object) -> str:
    arr = np.frombuffer(bytes(value), dtype=np.float32)
    return "[" + ",".join(f"{float(item):.8g}" for item in arr) + "]"


def _insert_sql(table: str, columns: list[str]) -> str:
    cols = ["kb_id", *columns]
    quoted = ",".join('"' + column.replace('"', '""') + '"' for column in cols)
    placeholders = ",".join(
        "?::vector" if table == "vectors" and column == "vec" else "?"
        for column in cols
    )
    return f"INSERT INTO {table}({quoted}) VALUES({placeholders}) ON CONFLICT DO NOTHING"


def migrate_kb(kb_id: str, sqlite_path: Path, *, verify: bool = False) -> list[Result]:
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    dest = connect(Path("unused.sqlite3"), kb_id=kb_id)
    init_schema(dest)
    results: list[Result] = []
    try:
        for table in TABLES:
            cols = _columns(src, table)
            if not cols:
                continue
            rows = src.execute(f"SELECT * FROM {table}").fetchall()
            sql = _insert_sql(table, cols)
            copied = 0
            skipped = 0
            with dest:
                for row in rows:
                    row_values = []
                    for col in cols:
                        value = row[col]
                        if table == "vectors" and col == "vec":
                            value = _vector_literal(value)
                        row_values.append(value)
                    values = (kb_id, *row_values)
                    cur = dest.execute(sql, values)
                    if int(getattr(cur, "rowcount", 1)) > 0:
                        copied += 1
                    else:
                        skipped += 1
            if verify:
                count = int(
                    dest.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE kb_id=?", (kb_id,)
                    ).fetchone()["c"]
                )
                if count < len(rows):
                    raise RuntimeError(
                        f"{kb_id}.{table}: destination {count} < source {len(rows)}"
                    )
            results.append(Result(table, len(rows), copied, skipped))
    finally:
        dest.close()
        src.close()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate department KB SQLite files to PostgreSQL")
    parser.add_argument("--data-dir", default="webchat/data")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    files = _discover(data_dir)
    if not files:
        print(f"没有找到 kb_*.sqlite3：{data_dir}", file=sys.stderr)
        return 2
    if args.dry_run:
        report = {}
        for kb_id, path in files.items():
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            try:
                report[kb_id] = {
                    table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                    for table in TABLES
                    if _columns(conn, table)
                }
            finally:
                conn.close()
    else:
        report = {
            kb_id: [r.__dict__ for r in migrate_kb(kb_id, path, verify=args.verify)]
            for kb_id, path in files.items()
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for kb_id, value in report.items():
            print(f"KB {kb_id}")
            if args.dry_run:
                for table, count in value.items():
                    print(f"  PLAN  {table:<18} {count}")
            else:
                for row in value:
                    print(
                        f"  COPY  {row['table']:<18} source={row['source_rows']} "
                        f"copied={row['copied_rows']} skipped={row['skipped_rows']}"
                    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
