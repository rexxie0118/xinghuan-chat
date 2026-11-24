"""Temporal memory graph and attachment tests."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from webchat import repository as repo
from webchat.db import connect, init_db
from webchat.user_memory import UserMemoryService


class TemporalMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="temporal-memory-"))
        self.db = self.tmp / "webchat.sqlite3"
        init_db(self.db)
        conn = connect(self.db)
        try:
            self.user = repo.user_public(repo.create_user(
                conn, username="temporal", display_name="T", password_hash="x"
            ))
        finally:
            conn.close()
        self.service = UserMemoryService(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_temporal_graph_keeps_historical_fact(self):
        first = self.service.remember(
            self.user["id"], kind="fact", content="项目负责人是张三",
            subject="项目", predicate="负责人", object_text="张三",
        )
        second = self.service.remember(
            self.user["id"], kind="fact", content="项目负责人是李四",
            subject="项目", predicate="负责人", object_text="李四",
        )
        graph = self.service.temporal_graph(self.user["id"], user=self.user)
        statuses = {edge["id"]: edge["status"] for edge in graph["edges"]}
        self.assertEqual(statuses[first["id"]], "historical")
        self.assertEqual(statuses[second["id"]], "current")
        self.assertGreaterEqual(len(graph["nodes"]), 2)

    def test_attachment_roundtrip(self):
        memory = self.service.remember(
            self.user["id"], kind="fact", content="带图片的记忆"
        )
        attachment = self.service.add_attachment(
            memory["id"],
            user_id=self.user["id"],
            kind="image",
            filename="demo.png",
            mime="image/png",
            path="/tmp/demo.png",
            caption="架构示意图",
        )
        items = self.service.attachments(memory["id"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], attachment["id"])
        self.assertEqual(items[0]["caption"], "架构示意图")
        self.assertEqual(self.service.get_attachment(attachment["id"])["filename"], "demo.png")


if __name__ == "__main__":
    unittest.main()
