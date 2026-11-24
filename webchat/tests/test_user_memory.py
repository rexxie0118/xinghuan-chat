"""Tests for per-user long-term memory."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from webchat import repository as repo
from webchat.db import connect, init_db
from webchat.user_memory import MemoryConflict, UserMemoryService, extract_candidates


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

    def test_department_and_org_scope_visibility(self):
        conn = connect(self.db)
        try:
            other = repo.create_user(
                conn, username="other", display_name="Other", password_hash="x",
                department="研发部",
            )
            admin = repo.create_user(
                conn, username="admin", display_name="Admin", password_hash="x",
                role="admin",
            )
        finally:
            conn.close()
        product = self.service.remember(
            self.user["id"], kind="fact", content="产品部共享经验",
            scope="department", scope_id="产品部",
        )
        org = self.service.remember(
            self.user["id"], kind="fact", content="全公司共享规范",
            scope="org", scope_id="org",
        )
        admin_user = repo.user_public(admin)
        self.service.approve(product["id"], approver=admin["id"], user=admin_user)
        self.service.approve(org["id"], approver=admin["id"], user=admin_user)
        other_recall = self.service.recall(
            other["id"], "共享", user=repo.user_public(other)
        )
        self.assertFalse(any(item["id"] == product["id"] for item in other_recall))
        self.assertTrue(any(item["id"] == org["id"] for item in other_recall))
        product_recall = self.service.recall(
            self.user["id"], "共享", user=repo.user_public(self.user)
        )
        self.assertTrue(any(item["id"] == product["id"] for item in product_recall))
        admin_recall = self.service.recall(
            admin["id"], "共享", user=repo.user_public(admin)
        )
        self.assertTrue(any(item["id"] == product["id"] for item in admin_recall))

    def test_provenance_is_stored(self):
        result = self.service.remember(
            self.user["id"], kind="fact", content="来源可追溯",
            source_uri="conversation:conv-1",
            evidence_snippet="用户原话：来源可追溯",
        )
        row = next(
            item for item in self.service.list(self.user["id"])
            if item["id"] == result["id"]
        )
        self.assertEqual(row["source_uri"], "conversation:conv-1")
        self.assertEqual(row["evidence_snippet"], "用户原话：来源可追溯")
        self.assertEqual(row["created_by"], self.user["id"])

    def test_shared_memory_requires_approval(self):
        conn = connect(self.db)
        try:
            manager = repo.create_user(
                conn, username="manager", display_name="Manager", password_hash="x",
                department="产品部", role="manager",
            )
        finally:
            conn.close()
        pending = self.service.remember(
            self.user["id"], kind="fact", content="产品部待审批经验",
            scope="department", scope_id="产品部",
        )
        self.assertEqual(pending["status"], "created")
        self.assertEqual(pending["snapshot"]["status"], "pending")
        self.assertEqual(self.service.recall(
            self.user["id"], "待审批", user=repo.user_public(self.user)
        ), [])
        approved = self.service.approve(
            pending["id"], approver=manager["id"], user=repo.user_public(manager)
        )
        self.assertEqual(approved["status"], "active")
        self.assertEqual(approved["approved_by"], manager["id"])
        self.assertTrue(self.service.recall(
            self.user["id"], "待审批", user=repo.user_public(self.user)
        ))

    def test_reject_shared_memory(self):
        conn = connect(self.db)
        try:
            manager = repo.create_user(
                conn, username="manager2", display_name="Manager", password_hash="x",
                department="产品部", role="manager",
            )
        finally:
            conn.close()
        pending = self.service.remember(
            self.user["id"], kind="fact", content="要拒绝的共享经验",
            scope="department", scope_id="产品部",
        )
        rejected = self.service.reject(
            pending["id"], approver=manager["id"], user=repo.user_public(manager)
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.service.pending(user=repo.user_public(manager)), [])

    def test_undo_deleted_memory(self):
        created = self.service.remember(
            self.user["id"], kind="fact", content="可撤销删除的记忆"
        )
        snapshot = created["snapshot"]
        self.assertTrue(self.service.forget(self.user["id"], created["id"]))
        self.assertEqual(self.service.list(self.user["id"]), [])
        restored = self.service.undo_event({"op": "delete", "snapshot": snapshot})
        self.assertEqual(restored["action"], "restore")
        self.assertTrue(any(item["id"] == created["id"] for item in self.service.list(self.user["id"])))

    def test_consolidate_all_runs_for_users(self):
        self.service.remember(self.user["id"], kind="fact", content="后台整理测试")
        result = self.service.consolidate_all(expire_days=0)
        self.assertGreaterEqual(result["users"], 1)
        self.assertIn("merged", result)

    def test_semantic_judge_merges_paraphrase(self):
        class Judge:
            def __init__(self):
                self.calls = []

            def same(self, left, right, kind):
                self.calls.append((left, right, kind))
                return "简短" in right

        judge = Judge()
        service = UserMemoryService(self.db, semantic_judge=judge)
        first = service.remember(
            self.user["id"], kind="preference", content="我偏好简洁回答"
        )
        second = service.remember(
            self.user["id"], kind="preference", content="回答请尽量简短"
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(judge.calls)
        self.assertEqual(len(service.list(self.user["id"])), 1)

    def test_pending_does_not_supersede_active(self):
        conn = connect(self.db)
        try:
            admin = repo.create_user(
                conn, username="admin2", display_name="Admin", password_hash="x",
                role="admin",
            )
            manager = repo.create_user(
                conn, username="manager3", display_name="Manager", password_hash="x",
                department="产品部", role="manager",
            )
        finally:
            conn.close()
        admin_user = repo.user_public(admin)
        manager_user = repo.user_public(manager)
        current = self.service.remember(
            self.user["id"], kind="fact", content="负责人是张三",
            subject="项目", predicate="负责人", object_text="张三",
            scope="department", scope_id="产品部",
        )
        self.service.approve(current["id"], approver=admin["id"], user=admin_user)
        pending = self.service.remember(
            self.user["id"], kind="fact", content="负责人是李四",
            subject="项目", predicate="负责人", object_text="李四",
            scope="department", scope_id="产品部",
        )
        before = self.service.recall(
            self.user["id"], "负责人", user=repo.user_public(self.user)
        )
        self.assertTrue(any(item["id"] == current["id"] for item in before))
        self.assertFalse(any(item["id"] == pending["id"] for item in before))
        with self.assertRaises(MemoryConflict):
            self.service.approve(pending["id"], approver=manager["id"], user=manager_user)

    def test_agent_owned_memory(self):
        self.service.remember(
            self.user["id"], kind="fact", content="Agent A 专属记忆",
            agent_id="agent-a",
        )
        self.service.remember(
            self.user["id"], kind="fact", content="通用个人记忆",
        )
        generic = self.service.recall(self.user["id"], "记忆", user=repo.user_public(self.user))
        self.assertFalse(any("Agent A" in item["content"] for item in generic))
        scoped = self.service.recall(
            self.user["id"], "记忆", user=repo.user_public(self.user), agent_id="agent-a"
        )
        self.assertTrue(any("Agent A" in item["content"] for item in scoped))

    def test_team_view_roles(self):
        conn = connect(self.db)
        try:
            manager = repo.create_user(
                conn, username="team_manager", display_name="M", password_hash="x",
                department="产品部", role="manager",
            )
            admin = repo.create_user(
                conn, username="team_admin", display_name="A", password_hash="x",
                role="admin",
            )
        finally:
            conn.close()
        self.service.remember(
            self.user["id"], kind="fact", content="部门团队记忆",
            scope="department", scope_id="产品部",
        )
        self.service.remember(
            self.user["id"], kind="fact", content="全公司团队记忆",
            scope="org", scope_id="org",
        )
        admin_user = repo.user_public(admin)
        manager_user = repo.user_public(manager)
        self.assertGreaterEqual(len(self.service.team(user=admin_user)), 2)
        manager_rows = self.service.team(user=manager_user)
        self.assertTrue(any(row["scope"] == "department" for row in manager_rows))
        self.assertEqual(self.service.team(user=repo.user_public(self.user)), [])

    def test_semantic_conflict_blocks_approval(self):
        class Judge:
            def relation(self, left, right, kind="fact"):
                return "conflict"

            def same(self, left, right, kind="fact"):
                return False

        conn = connect(self.db)
        try:
            admin = repo.create_user(
                conn, username="conflict_admin", display_name="A", password_hash="x",
                role="admin",
            )
        finally:
            conn.close()
        admin_user = repo.user_public(admin)
        service = UserMemoryService(self.db, semantic_judge=Judge())
        active = service.remember(
            self.user["id"], kind="fact", content="负责人是张三",
            subject="项目", predicate="负责人", object_text="张三",
            scope="department", scope_id="产品部",
        )
        service.approve(active["id"], approver=admin["id"], user=admin_user)
        pending = service.remember(
            self.user["id"], kind="fact", content="负责人是李四",
            subject="项目", predicate="负责人", object_text="李四",
            scope="department", scope_id="产品部",
        )
        with self.assertRaises(MemoryConflict):
            service.approve(pending["id"], approver=admin["id"], user=admin_user)

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
