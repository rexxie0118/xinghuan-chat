"""Tests for external Agent tool-loop protocol."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from webchat.external_agent import ExternalAgentClient
from webchat.tools import ToolResult, ToolSpec


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeRegistry:
    def __init__(self):
        self.specs = [
            ToolSpec(
                name="demo.echo",
                description="Echo",
                input_schema={"type": "object"},
                server="plugin",
                raw_name="echo",
            )
        ]
        self.calls = []

    def list_tools(self):
        return self.specs

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult(ok=True, tool=name, content="echo:" + arguments.get("text", ""))


class ExternalAgentTest(unittest.TestCase):
    def settings(self):
        return SimpleNamespace(
            agent_url="http://agent.local",
            agent_token="",
            agent_timeout=5,
        )

    def test_tool_call_then_answer(self):
        responses = [
            {"tool_calls": [{"id": "c1", "function": {"name": "demo.echo", "arguments": "{\"text\": \"hi\"}"}}]},
            {"answer": "使用工具后回答"},
        ]
        def poster(*args, **kwargs):
            return FakeResponse(responses.pop(0))
        registry = FakeRegistry()
        client = ExternalAgentClient(self.settings(), registry, poster=poster)
        result = client.run(
            question="测试",
            mode="agent",
            user={"id": "u1", "display_name": "U"},
            conversation={"id": "c1"},
            history=[],
            retrieval={"hits": [], "graph": {}},
            kb=SimpleNamespace(info=SimpleNamespace(id="kb", name="KB", department="产品部")),
        )
        self.assertEqual(result["answer"], "使用工具后回答")
        self.assertEqual(result["tools_used"], ["demo.echo"])
        self.assertEqual(registry.calls, [("demo.echo", {"text": "hi"})])
        self.assertEqual(result["steps"], 2)

    def test_legacy_answer_still_works(self):
        client = ExternalAgentClient(
            self.settings(),
            None,
            poster=lambda *args, **kwargs: FakeResponse({"answer": "直接回答"}),
        )
        result = client.run(
            question="测试",
            mode="agent",
            user={"id": "u1", "display_name": "U"},
            conversation={"id": "c1"},
            history=[],
            retrieval={"hits": [], "graph": {}},
            kb=SimpleNamespace(info=SimpleNamespace(id="kb", name="KB", department="产品部")),
        )
        self.assertEqual(result["answer"], "直接回答")
        self.assertEqual(result["tools_used"], [])


if __name__ == "__main__":
    unittest.main()
