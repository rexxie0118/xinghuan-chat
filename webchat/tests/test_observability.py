"""Tests for audit persistence and request correlation."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webchat.config import ROOT, Settings
from webchat.db import init_db
from webchat.main import create_app
from webchat.observability import AuditLog, request_id_var
from webchat.tools import ToolRegistry


class AuditLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="audit-test-"))
        self.db = self.tmp / "webchat.sqlite3"
        init_db(self.db)
        self.audit = AuditLog(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_query_metrics(self):
        request_id_var.set("req_test")
        self.audit.record(
            event_type="tool_call",
            component="plugin",
            action="demo.echo",
            status="ok",
            duration_ms=12,
            user_id="u1",
            metadata={"argument_keys": ["text"]},
        )
        self.audit.record(
            event_type="chat_run",
            component="agent",
            action="agent",
            status="error",
            duration_ms=40,
            failure_class="timeout",
            user_id="u1",
        )
        events = self.audit.query(user_id="u1")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["request_id"], "req_test")
        metrics = self.audit.metrics(user_id="u1")
        self.assertEqual(metrics["events"], 2)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(metrics["failures"]["timeout"], 1)

    def test_tool_registry_writes_audit(self):
        audit = self.audit
        registry = ToolRegistry({}, audit=audit)
        registry.register_local(
            name="demo.echo",
            description="echo",
            input_schema={"type": "object"},
            handler=lambda _args: {"content": "ok"},
        )
        registry.call("demo.echo", {})
        events = audit.query(event_type="tool_call")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action"], "demo.echo")
        self.assertNotIn("secret", str(events[0]["metadata"]))


class RequestIdTest(unittest.TestCase):
    def test_request_id_header_and_audit(self):
        tmp = Path(tempfile.mkdtemp(prefix="audit-api-"))
        settings = Settings(
            root=ROOT,
            data_dir=tmp,
            db_path=tmp / "webchat.sqlite3",
            kb_path=tmp / "kb.sqlite3",
            kb_config_path=ROOT / "kb.config.json",
            knowledge_dir=ROOT / "knowledge",
            static_dir=ROOT / "static",
            agent_url=None,
            agent_token=None,
            agent_timeout=10,
            session_days=7,
            secure_cookie=False,
            kb_provider="mock",
            seed_demo_users=True,
        )
        app = create_app(settings)
        try:
            with TestClient(app) as client:
                resp = client.get("/api/meta", headers={"X-Request-ID": "req-from-client"})
                self.assertEqual(resp.headers.get("X-Request-ID"), "req-from-client")
            events = AuditLog(settings.db_path).query(event_type="api_request", limit=20)
            self.assertTrue(any(event["request_id"] == "req-from-client" for event in events))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
