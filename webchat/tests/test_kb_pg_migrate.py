"""Tests for the shared PostgreSQL KB schema/migration planner."""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from kb.db import PG_SCHEMA, init_schema
from webchat.kb_pg_migrate import _discover, _insert_sql


class KbPostgresTest(unittest.TestCase):
    def test_pg_schema_is_kb_scoped_and_rls(self):
        text = "\n".join(PG_SCHEMA)
        self.assertIn("kb_id", text)
        self.assertIn("ROW LEVEL SECURITY", text)
        self.assertIn("vector", text.lower())
        self.assertIn("chunk_fts", text)

    def test_migration_plan_discovers_kb_files(self):
        tmp = Path(tempfile.mkdtemp(prefix="kb-pg-plan-"))
        try:
            db = tmp / "kb_product.sqlite3"
            from kb.db import connect
            conn = connect(db)
            init_schema(conn)
            try:
                conn.execute(
                    "INSERT INTO documents(id,source_uri,title,doc_type,content,content_hash,"
                    "created_at,updated_at,meta_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    ("d1", "file:///t", "T", "note", "hello", "h", "now", "now", "{}"),
                )
                conn.commit()
                files = _discover(tmp)
                self.assertEqual(files, {"product": db})
                sql = _insert_sql("documents", ["id", "title"])
                self.assertIn('"id"', sql)
                self.assertIn("ON CONFLICT DO NOTHING", sql)
            finally:
                conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
