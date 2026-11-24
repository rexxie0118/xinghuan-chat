"""Copy the full WebChat SQLite database into PostgreSQL.

Usage:
    WEBCHAT_DB_URL=postgresql://user:pass@host/db \
      python -m webchat.pg_migrate --sqlite data/webchat.sqlite3 --verify

The destination schema is created/upgraded with ``init_db``. Existing rows are
updated by primary key by default; use ``--mode skip`` to leave conflicts alone.
Passwords/DSNs are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import DB_URL_ENV, PgConnection, init_db

# Foreign-key-safe copy order.
TABLES: list[tuple[str, str]] = [
    ("users", "id"),
    ("sessions", "token_hash"),
    ("conversations", "id"),
    ("messages", "id"),
    ("agents", "id"),
    ("agent_runs", "id"),
    ("user_runtimes", "user_id"),
    ("container_workspaces", "conversation_id"),
    ("container_checkpoints", "conversation_id"),
    ("user_memories", "id"),
    ("connectors", "id"),
    ("audit_events", "id"),
    ("department_agents", "id"),
]


@dataclass
class TableResult:
    table: str
    source_rows: int
    copied_rows: int
    skipped_rows: int = 0


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _sqlite_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [dict(row) for row in rows]


def _insert_sql(table: str, columns: list[str], pk: str, mode: str) -> str:
    placeholders = ",".join("?" for _ in columns)
    sql = f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})"
    sql += f" ON CONFLICT({pk}) DO NOTHING" if mode == "skip" else (
        f" ON CONFLICT({pk}) DO UPDATE SET "
        + ", ".join(f"{column}=excluded.{column}" for column in columns if column != pk)
    )
    return sql


def _source_plan(sqlite_path: Path) -> list[tuple[str, str, list[str], int]]:
    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    try:
        plan = []
        for table, pk in TABLES:
            columns = _sqlite_columns(src, table)
            if not columns:
                continue
            count = src.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
            plan.append((table, pk, columns, int(count)))
        return plan
    finally:
        src.close()


def migrate(
    sqlite_path: Path,
    dsn: str,
    *,
    mode: str = "upsert",
    dry_run: bool = False,
    batch_size: int = 500,
    verify: bool = False,
) -> dict[str, Any]:
    if not sqlite_path.exists():
        raise FileNotFoundError(f"SQLite 数据库不存在：{sqlite_path}")
    plan = _source_plan(sqlite_path)
    if dry_run:
        return {
            "dry_run": True,
            "tables": [
                {"table": table, "rows": rows, "columns": columns, "pk": pk}
                for table, pk, columns, rows in plan
            ],
        }

    # init_db reads WEBCHAT_DB_URL from the environment. Set it for this process
    # only; the CLI also validates it before invoking migration.
    previous = os.environ.get(DB_URL_ENV)
    os.environ[DB_URL_ENV] = dsn
    try:
        init_db(Path("unused.sqlite3"))
    finally:
        if previous is None:
            os.environ.pop(DB_URL_ENV, None)
        else:
            os.environ[DB_URL_ENV] = previous

    src = sqlite3.connect(str(sqlite_path))
    src.row_factory = sqlite3.Row
    dest = PgConnection(dsn)
    results: list[TableResult] = []
    try:
        for table, pk, columns, _count in plan:
            rows = _sqlite_rows(src, table)
            copied = 0
            skipped = 0
            sql = _insert_sql(table, columns, pk, mode)
            with dest:
                for row in rows:
                    values = tuple(row.get(column) for column in columns)
                    cursor = dest.execute(sql, values)
                    if int(getattr(cursor, "rowcount", 1)) > 0:
                        copied += 1
                    else:
                        skipped += 1
            if verify:
                destination_rows = int(
                    dest.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                )
                if destination_rows < len(rows):
                    raise RuntimeError(
                        f"{table}: destination rows {destination_rows} < source {len(rows)}"
                    )
            results.append(TableResult(table, len(rows), copied, skipped))
    finally:
        dest.close()
        src.close()
    return {
        "dry_run": False,
        "tables": [result.__dict__ for result in results],
        "source": str(sqlite_path),
        "total_rows": sum(result.source_rows for result in results),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate Xinghuan Chat SQLite to PostgreSQL")
    parser.add_argument("--sqlite", default="data/webchat.sqlite3", help="source SQLite path")
    parser.add_argument("--postgres", default="", help="destination DSN; defaults to WEBCHAT_DB_URL")
    parser.add_argument("--mode", choices=["upsert", "skip"], default="upsert")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true", help="check destination counts")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    dsn = args.postgres or (os.getenv(DB_URL_ENV) or "").strip()
    if not dsn and not args.dry_run:
        print("缺少 PostgreSQL DSN：设置 WEBCHAT_DB_URL 或 --postgres", file=sys.stderr)
        return 2
    try:
        report = migrate(
            Path(args.sqlite), dsn or "postgresql://dry-run", mode=args.mode,
            dry_run=args.dry_run, verify=args.verify,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"迁移失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        dry_run = bool(report.get("dry_run"))
        for table in report["tables"]:
            if dry_run:
                print(f"PLAN  {table['table']:<24} {table['rows']} rows")
            else:
                print(
                    f"COPY  {table['table']:<24} source={table['source_rows']} "
                    f"copied={table['copied_rows']} skipped={table['skipped_rows']}"
                )
        if not report.get("dry_run"):
            print(f"TOTAL {report['total_rows']} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
