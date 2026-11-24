"""Tests for the SQLite -> PostgreSQL migration planner/copy loop."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from webchat import repository as repo
from webchat.db import init_db
from webchat.pg_migrate import TABLES, _insert_sql, _source_plan, migrate


class PgMigrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pg-migrate-test-"))
        self.db = self.tmp / "webchat.sqlite3"
        init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            user = repo.create_user(
                conn, username="migrate", display_name="M", password_hash="x"
            )
            conversation = repo.create_conversation(conn, user_id=user["id"], mode="rag")
            repo.add_message(
                conn,
                conversation_id=conversation["id"],
                user_id=user["id"],
                role="user",
                content="迁移测试",
            )
        finally:
            conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_plan_covers_all_tables(self):
        report = migrate(self.db, "", dry_run=True)
        names = {item["table"] for item in report["tables"]}
        self.assertEqual(names, {table for table, _ in TABLES})
        memory_plan = next(item for item in report["tables"] if item["table"] == "user_memories")
        self.assertIn("scope", memory_plan["columns"] if isinstance(memory_plan["columns"], list) else [])
        self.assertTrue(report["dry_run"])

    def test_insert_sql_uses_postgres_upsert(self):
        sql = _insert_sql("users", ["id", "username"], "id", "upsert")
        self.assertIn("ON CONFLICT(id) DO UPDATE", sql)
        self.assertIn("excluded.username", sql)
        skip = _insert_sql("users", ["id", "username"], "id", "skip")
        self.assertIn("DO NOTHING", skip)

    def test_copy_loop_uses_destination(self):
        class Cursor:
            rowcount = 1

        class FakeDest:
            def __init__(self, *_args, **_kwargs):
                self.sql = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=()):
                self.sql.append((sql, params))
                return Cursor()

            def close(self):
                pass

        fake = FakeDest()
        with patch("webchat.pg_migrate.PgConnection", return_value=fake), patch(
            "webchat.pg_migrate.init_db"
        ):
            report = migrate(self.db, "postgresql://secret@example/db", mode="skip")
        self.assertFalse(report["dry_run"])
        self.assertGreaterEqual(report["total_rows"], 3)
        self.assertTrue(any("INSERT INTO users" in sql for sql, _ in fake.sql))
        self.assertIn("users", {item["table"] for item in report["tables"]})


if __name__ == "__main__":
    unittest.main()
