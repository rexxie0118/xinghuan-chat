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

    def test_low_quality_auto_write_is_skipped(self):
        result = self.service.remember(
            self.user["id"], kind="fact", content="临时", confidence=0.2, auto=True
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.service.list(self.user["id"]), [])

    def test_near_duplicate_merges(self):
        first = self.service.remember(
            self.user["id"], kind="preference", content="我偏好简洁回答"
        )
        second = self.service.remember(
            self.user["id"], kind="preference", content="我偏好简洁回答。"
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.service.list(self.user["id"])), 1)

    def test_consolidate_archives_duplicate_rows(self):
        now = "2026-11-24T00:00:00+00:00"
        conn = connect(self.db)
        try:
            with conn:
                for i in (1, 2):
                    conn.execute(
                        "INSERT INTO user_memories(id, user_id, kind, content, subject,"
                        " predicate, object_text, importance, confidence, hits,"
                        " source_conversation_id, source_message_id, created_at,"
                        " updated_at, last_used_at, valid_from, valid_to, superseded_by)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"legacy{i}", self.user["id"], "fact", "回答尽量简洁",
                            "", "", "回答尽量简洁", 0.5, 0.7, 0,
                            "", "", now, now, now, now, None, "",
                        ),
                    )
        finally:
            conn.close()
        result = self.service.consolidate(self.user["id"])
        self.assertEqual(result["merged"], 1)
        self.assertEqual(result["stats"]["active"], 1)
        self.assertEqual(result["stats"]["superseded"], 1)

    def test_recall_respects_char_budget(self):
        for index in range(6):
            self.service.remember(
                self.user["id"],
                kind="preference",
                content=f"偏好{index}：" + "很长的记忆内容" * 10,
            )
        recalled = self.service.recall(self.user["id"], "偏好", k=6, max_chars=80)
        self.assertLessEqual(sum(len(item["content"]) for item in recalled), 80)
        self.assertLessEqual(len(recalled), 1)

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
