"""Tests for declarative connector validation/store/executor."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from webchat.connectors import (
    ConnectorError,
    ConnectorExecutor,
    ConnectorService,
    ConnectorStore,
    known_for,
    parse_spec,
)
from webchat.db import connect, init_db
from webchat.tools import ToolRegistry

VALID = {
    "name": "demo_weather",
    "description": "demo weather",
    "base_url": "https://api.example.com/v1",
    "method": "GET",
    "path": "/weather",
    "params": {"city": {"type": "string", "description": "城市"}},
    "response_path": "current",
    "allow_hosts": ["api.example.com"],
}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def get(self, url, params=None, headers=None):
        return httpx.Response(
            200,
            json={"current": {"temperature": 22}},
            request=httpx.Request("GET", url),
        )

    def post(self, *args, **kwargs):
        return self.get("https://api.example.com/v1/weather")

    def close(self):
        self.closed = True


class ConnectorTest(unittest.TestCase):
    def test_parse_rejects_http_and_unknown_host(self):
        with self.assertRaises(ConnectorError):
            parse_spec({**VALID, "base_url": "http://api.example.com"})
        with self.assertRaises(ConnectorError):
            parse_spec({**VALID, "allow_hosts": ["other.example.com"]})
        spec = parse_spec(VALID)
        self.assertEqual(spec.tool_name, "connector.demo_weather")

    def test_executor_maps_response_path(self):
        spec = parse_spec(VALID)
        executor = ConnectorExecutor(client_factory=FakeClient)
        result = executor.execute(spec, {"city": "北京"})
        self.assertEqual(result["structured"]["temperature"], 22)
        self.assertIn("temperature", result["content"])
        self.assertEqual(result["url"].split("?")[0], "https://api.example.com/v1/weather")

    def test_store_approve_and_register_tool(self):
        tmp = Path(tempfile.mkdtemp(prefix="connector-test-"))
        try:
            db = tmp / "db.sqlite3"
            init_db(db)
            conn = connect(db)
            try:
                conn.execute(
                    "INSERT INTO users(id, username, display_name, password_hash, created_at)"
                    " VALUES(?,?,?,?,?)",
                    ("u1", "u1", "U1", "x", "now"),
                )
                conn.commit()
            finally:
                conn.close()
            store = ConnectorStore(db)
            spec = parse_spec(VALID)
            draft = store.create("u1", spec)
            self.assertEqual(draft["status"], "draft")
            approved = store.approve(draft["id"], "u1")
            self.assertEqual(approved["status"], "approved")
            tools = ToolRegistry({})
            service = ConnectorService(SimpleNamespace(db_path=db), tools)
            service.register(spec)
            self.assertIn("connector.demo_weather", [t.name for t in tools.list_tools()])
            self.assertEqual(known_for("天气")[0]["name"], "weather_geocode")
            self.assertEqual(known_for("天气")[0]["params"]["city"]["key"], "name")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
