"""Tests for per-user long-term memory."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from webchat import repository as repo
from webchat.db import connect, init_db
from webchat.user_memory import UserMemoryService, extract_candidates


class UserMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="umem-test-"))
        self.db = self.tmp / "webchat.sqlite3"
        init_db(self.db)
        conn = connect(self.db)
        try:
            self.user = repo.create_user(
                conn, username="memory", display_name="Memory", password_hash="x"
            )
        finally:
            conn.close()
        self.service = UserMemoryService(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_and_recall(self):
        text = "我叫林晓，我更喜欢简洁回答。请记住：项目代号北极星。"
        candidates = extract_candidates(text)
        self.assertTrue(any(c["predicate"] == "名字" for c in candidates))
        self.assertTrue(any(c["kind"] == "preference" for c in candidates))
        results = self.service.extract_and_remember(self.user["id"], text)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(self.service.list(self.user["id"])), 3)
        recalled = self.service.recall(self.user["id"], "我叫什么？")
        self.assertTrue(recalled)
        self.assertTrue(any("林晓" in item["content"] for item in recalled))

    def test_slot_supersede_and_forget(self):
        self.service.remember(
            self.user["id"], kind="fact", content="我的名字是张三",
            subject="用户", predicate="名字", object_text="张三",
        )
        self.service.remember(
            self.user["id"], kind="fact", content="我的名字是李四",
            subject="用户", predicate="名字", object_text="李四",
        )
        current = self.service.list(self.user["id"])
        self.assertEqual(len(current), 1)
        self.assertIn("李四", current[0]["content"])
        self.assertTrue(self.service.forget(self.user["id"], current[0]["id"]))
        self.assertEqual(self.service.list(self.user["id"]), [])


if __name__ == "__main__":
    unittest.main()
