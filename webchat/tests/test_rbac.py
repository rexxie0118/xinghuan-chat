"""Tests for department-scoped KB and default department Agents."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from webchat import repository as repo
from webchat.agent_defs import get_agent, list_agents, seed_department_agents
from webchat.db import connect, init_db
from webchat.knowledge import KnowledgeRegistry


class RbacTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rbac-test-"))
        self.db = self.tmp / "webchat.sqlite3"
        init_db(self.db)
        conn = connect(self.db)
        try:
            self.product = repo.create_user(
                conn, username="product", display_name="P", password_hash="x",
                department="产品部",
            )
            self.engineering = repo.create_user(
                conn, username="eng", display_name="E", password_hash="x",
                department="研发部",
            )
            self.admin = repo.create_user(
                conn, username="admin", display_name="A", password_hash="x",
                department="产品部", role="admin",
            )
            seed_department_agents(conn)
        finally:
            conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _user(self, row):
        return repo.user_public(row)

    def test_department_agents_are_scoped(self):
        conn = connect(self.db)
        try:
            product = self._user(repo.get_user(conn, self.product["id"]))
            eng = self._user(repo.get_user(conn, self.engineering["id"]))
            admin = self._user(repo.get_user(conn, self.admin["id"]))
            p_names = {a["name"] for a in list_agents(conn, product)}
            e_names = {a["name"] for a in list_agents(conn, eng)}
            a_names = {a["name"] for a in list_agents(conn, admin)}
            self.assertIn("产品知识助手", p_names)
            self.assertNotIn("研发知识助手", p_names)
            self.assertIn("研发知识助手", e_names)
            self.assertIn("产品知识助手", a_names)
            self.assertIn("人事政策助手", a_names)
            self.assertIsNotNone(get_agent(conn, "dept_product", product))
            self.assertIsNone(get_agent(conn, "dept_engineering", product))
        finally:
            conn.close()

    def test_default_kb_prefers_department(self):
        registry = object.__new__(KnowledgeRegistry)
        registry.default_department = "产品部"
        registry.services = {
            "common": SimpleNamespace(
                info=SimpleNamespace(id="common", department="all", is_shared=True)
            ),
            "product": SimpleNamespace(
                info=SimpleNamespace(id="product", department="产品部", is_shared=False)
            ),
            "engineering": SimpleNamespace(
                info=SimpleNamespace(id="engineering", department="研发部", is_shared=False)
            ),
        }
        registry.infos = [svc.info for svc in registry.services.values()]
        product = {"department": "产品部", "role": "member"}
        eng = {"department": "研发部", "role": "member"}
        self.assertEqual(registry.default_for(product).info.id, "product")
        self.assertEqual(registry.default_for(eng).info.id, "engineering")
        self.assertEqual(registry.default_for({"department": "研发部", "role": "admin"}).info.id, "engineering")


if __name__ == "__main__":
    unittest.main()
