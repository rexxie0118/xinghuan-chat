"""Tests for MCP shard pooling and failover."""
from __future__ import annotations

import unittest

from webchat.tools import MCPClientPool, ToolError


class FakeClient:
    def __init__(self, name, *, fail=False):
        self.name = name
        self.fail = fail
        self.calls = 0
        self.closed = False

    def list_tools(self):
        return [{"name": "ping", "description": "ping"}]

    def call_tool(self, name, arguments):
        self.calls += 1
        if self.fail:
            raise ToolError("shard down")
        return {"content": f"{self.name}:{arguments.get('x')}"}

    def close(self):
        self.closed = True


class ClientPoolTest(unittest.TestCase):
    def test_round_robin(self):
        a, b = FakeClient("a"), FakeClient("b")
        pool = MCPClientPool("server", [a, b])
        self.assertIn("a", pool.call_tool("ping", {"x": 1})["content"])
        self.assertIn("b", pool.call_tool("ping", {"x": 2})["content"])
        self.assertEqual(pool.public()["shards"], 2)
        self.assertEqual(pool.public()["calls"], 2)

    def test_failover_to_next_shard(self):
        bad, good = FakeClient("bad", fail=True), FakeClient("good")
        pool = MCPClientPool("server", [bad, good])
        result = pool.call_tool("ping", {"x": 1})
        self.assertIn("good", result["content"])
        self.assertEqual(pool.public()["failures"], 1)
        pool.close()
        self.assertTrue(bad.closed and good.closed)


if __name__ == "__main__":
    unittest.main()
