"""Tests for per-user runtime and per-conversation workspace mapping."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from webchat import repository as repo
from webchat.container_workspaces import ContainerWorkspaceService, runtime_key, workspace_path
from webchat.db import connect, init_db
from webchat.sandbox.apple_runtime import RuntimeInfo


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.stopped = False

    def ensure(self, key, **_kwargs):
        self.calls.append(("ensure", key))
        return RuntimeInfo(key, "xh-rt-test", "xh-vol-test", "python:3.12-slim", "running", "xh-rt-test")

    def exec(self, key, command, **_kwargs):
        self.calls.append(("exec", key, command))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def stop(self, key):
        self.calls.append(("stop", key))
        return RuntimeInfo(key, "xh-rt-test", "xh-vol-test", "python:3.12-slim", "stopped", "xh-rt-test")

    def delete(self, key, **_kwargs):
        self.calls.append(("delete", key))
        return True


class ContainerWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cw-test-"))
        self.db = self.tmp / "webchat.sqlite3"
        init_db(self.db)
        conn = connect(self.db)
        try:
            self.user = repo.create_user(
                conn, username="cwuser", display_name="CW", password_hash="x"
            )
            self.conv = repo.create_conversation(
                conn, user_id=self.user["id"], mode="agent", kb_id="product"
            )
        finally:
            conn.close()
        self.runtime = FakeRuntime()
        self.service = ContainerWorkspaceService(
            SimpleNamespace(db_path=self.db), self.runtime
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_user_runtime_and_workspace_mapping(self):
        info = self.service.ensure_workspace(self.user["id"], self.conv["id"])
        self.assertEqual(info["runtime_key"], runtime_key(self.user["id"]))
        self.assertEqual(info["state"], "running")
        self.assertEqual(info["workspace_path"], workspace_path(self.conv["id"]))
        self.assertIn(("ensure", runtime_key(self.user["id"])), self.runtime.calls)
        self.assertIn(
            ("exec", runtime_key(self.user["id"]), ["mkdir", "-p", workspace_path(self.conv["id"])]),
            self.runtime.calls,
        )
        mapping = self.service.get(self.user["id"])
        self.assertEqual(mapping["runtime"]["user_id"], self.user["id"])
        self.assertEqual(len(mapping["workspaces"]), 1)
        self.assertEqual(mapping["workspaces"][0]["conversation_id"], self.conv["id"])

    def test_stop_and_delete(self):
        self.service.ensure_user(self.user["id"])
        stopped = self.service.stop(self.user["id"])
        self.assertEqual(stopped["runtime"]["state"], "stopped")
        self.service.delete(self.user["id"])
        self.assertIsNone(self.service.get(self.user["id"]))
        self.assertIn(("delete", runtime_key(self.user["id"])), self.runtime.calls)


if __name__ == "__main__":
    unittest.main()
