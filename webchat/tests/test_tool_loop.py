"""Tests for the real LLM tool-calling loop."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from webchat.agent import AgentRunner
from webchat.tools import ToolResult, ToolSpec


class FakeChat:
    def __init__(self):
        self.calls = 0
        self.seen_tools = []

    def complete_with_tools(self, messages, tools, **_kwargs):
        self.calls += 1
        self.seen_tools = tools
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "demo_echo", "arguments": '{"text": "hi"}'},
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {
            "content": "最终回答 [1]",
            "tool_calls": [],
            "finish_reason": "stop",
        }


class FakeRegistry:
    def __init__(self):
        self.specs = [
            ToolSpec(
                name="demo.echo",
                description="Echo text",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                server="plugin",
                raw_name="echo",
            )
        ]
        self.calls = []

    def list_tools(self):
        return self.specs

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return ToolResult(ok=True, tool=name, content="echo:" + arguments["text"], server="plugin")


class FakeProvider:
    is_mock = False
    def __init__(self):
        self.chat = FakeChat()


class FakeKB:
    def __init__(self):
        self.provider = FakeProvider()
        self.info = SimpleNamespace(id="product", name="产品知识库")

    def suggestions(self, *_args):
        return []


class ToolLoopTest(unittest.TestCase):
    def test_loop_calls_tool_and_returns_final_answer(self):
        settings = SimpleNamespace(tool_loop_enabled=True, tool_max_steps=4)
        runner = AgentRunner(settings, sandbox=None, tool_registry=FakeRegistry())
        kb = FakeKB()
        result = {
            "hits": [
                {
                    "chunk_id": "c1",
                    "doc_id": "d1",
                    "doc_title": "测试文档",
                    "heading_path": "测试",
                    "text": "知识库证据",
                    "score": 0.1,
                }
            ],
            "graph": {"nodes": [], "edges": []},
            "stats": {"timings": {}},
        }
        answer = runner._run_tool_loop(
            question="测试工具",
            history=[],
            result=result,
            base_trace=[],
            kb=kb,
            memories=[],
            status=lambda *_args: None,
            cancel_token=None,
        )
        self.assertIsNotNone(answer)
        self.assertEqual(answer.answer, "最终回答 [1]")
        self.assertEqual(answer.meta["answer_mode"], "tool_loop")
        self.assertEqual(answer.meta["tool_steps"], 1)
        self.assertEqual(answer.meta["tools_used"], ["demo.echo"])
        self.assertEqual(answer.citations[0]["index"], 1)
        self.assertEqual(kb.provider.chat.calls, 2)
        self.assertEqual(kb.provider.chat.seen_tools[0]["function"]["name"], "demo_echo")
        self.assertEqual(runner.tool_registry.calls, [("demo.echo", {"text": "hi"})])


if __name__ == "__main__":
    unittest.main()
