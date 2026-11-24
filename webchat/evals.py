"""Small deterministic eval suite for RAG/tooling/failure behavior.

Run:
    python -m webchat.evals
    python -m webchat.evals --json
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable

from .agent import AgentRunner
from .connectors import ConnectorError, parse_spec
from .external_agent import ExternalAgentClient
from .runtime.injection import FailureInjector, FailureRule, InjectedFailure
from .tools import ToolRegistry, ToolResult, ToolSpec
from .websearch import conflict_detect, source_rank


@dataclass
class EvalResult:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


def _rag_citations() -> str:
    result = {
        "hits": [
            {"chunk_id": "c1", "doc_id": "d1", "doc_title": "A", "text": "a"},
            {"chunk_id": "c2", "doc_id": "d2", "doc_title": "B", "text": "b"},
        ]
    }
    citations = AgentRunner._citations_from_indices([1, 2, 9], result)
    assert [c["index"] for c in citations] == [1, 2], citations
    return "valid indices retained; out-of-range dropped"


class _FakeChat:
    def __init__(self):
        self.calls = 0

    def complete_with_tools(self, messages, tools, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "demo_echo", "arguments": '{"text":"hi"}'},
                    }
                ],
            }
        return {"content": "最终回答 [1]", "tool_calls": []}


class _FakeTools:
    def list_tools(self):
        return [
            ToolSpec(
                name="demo.echo",
                description="echo",
                input_schema={"type": "object"},
                server="plugin",
                raw_name="echo",
            )
        ]

    def call(self, name, arguments):
        return ToolResult(ok=True, tool=name, content="echo:" + arguments["text"])


class _FakeKB:
    def __init__(self):
        self.provider = SimpleNamespace(is_mock=False, chat=_FakeChat())
        self.info = SimpleNamespace(id="product", name="产品")

    def suggestions(self, *_args):
        return []


def _tool_loop() -> str:
    runner = AgentRunner(
        SimpleNamespace(tool_loop_enabled=True, tool_max_steps=4),
        sandbox=None,
        tool_registry=_FakeTools(),
    )
    result = {
        "hits": [
            {
                "chunk_id": "c1",
                "doc_id": "d1",
                "doc_title": "A",
                "heading_path": "",
                "text": "evidence",
                "score": 0.1,
            }
        ],
        "graph": {"nodes": [], "edges": []},
        "stats": {"timings": {}},
    }
    answer = runner._run_tool_loop(
        question="tool?",
        history=[],
        result=result,
        base_trace=[],
        kb=_FakeKB(),
        memories=[],
        status=lambda *_args: None,
        cancel_token=None,
    )
    assert answer is not None and answer.meta["tool_steps"] == 1, answer
    assert answer.meta["tools_used"] == ["demo.echo"]
    return "tool_calls -> ToolRegistry -> final answer"


def _web_rank_and_conflict() -> str:
    ranked = source_rank(
        [
            {
                "title": "官方",
                "url": "https://www.cma.gov.cn/a",
                "snippet": "0℃",
                "published_at": "2026-11-24T08:00:00+00:00",
                "domain": "www.cma.gov.cn",
            },
            {
                "title": "普通",
                "url": "https://example.com/b",
                "snippet": "20℃",
                "domain": "example.com",
            },
        ]
    )
    assert ranked[0]["domain"] == "www.cma.gov.cn"
    conflicts = conflict_detect(
        [
            {"title": "价格 10 元", "snippet": "价格 10 元", "domain": "a", "url": "https://a"},
            {"title": "价格 12 元", "snippet": "价格 12 元", "domain": "b", "url": "https://b"},
        ]
    )
    assert conflicts["conflict"] is True
    return f"official rank={ranked[0]['trust_score']}; numeric conflict detected"


def _external_react() -> str:
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    responses = [
        {"tool_calls": [{"function": {"name": "demo.echo", "arguments": '{"text":"x"}'}}]},
        {"answer": "外部 Agent 完成"},
    ]
    client = ExternalAgentClient(
        SimpleNamespace(agent_url="http://agent.local", agent_token="", agent_timeout=5),
        _FakeTools(),
        poster=lambda *args, **kwargs: Response(responses.pop(0)),
        injector=FailureInjector(),
    )
    data = client.run(
        question="q",
        mode="agent",
        user={"id": "u", "display_name": "U"},
        conversation={"id": "c"},
        history=[],
        retrieval={"hits": [], "graph": {}},
        kb=SimpleNamespace(info=SimpleNamespace(id="p", name="P", department="D")),
    )
    assert data and data["answer"] == "外部 Agent 完成"
    assert data["tools_used"] == ["demo.echo"]
    return "external tool_calls -> observations -> answer"


def _failure_injection() -> str:
    registry = ToolRegistry(
        {},
        injector=FailureInjector([FailureRule("tool", "demo.echo", times=1)]),
    )
    registry.register_local(
        name="demo.echo",
        description="echo",
        input_schema={"type": "object"},
        handler=lambda _args: {"content": "ok"},
    )
    try:
        registry.call("demo.echo", {})
        raise AssertionError("expected injected failure")
    except InjectedFailure as exc:
        assert exc.component == "tool"
    result = registry.call("demo.echo", {})
    assert result.ok
    return "first tool call injected; second succeeds"


def _permission_gate() -> str:
    spec = {
        "name": "bad",
        "description": "bad",
        "base_url": "http://example.com",
        "method": "GET",
        "path": "/",
        "allow_hosts": ["example.com"],
    }
    try:
        parse_spec(spec)
        raise AssertionError("expected HTTP rejection")
    except ConnectorError:
        pass
    return "non-HTTPS connector rejected"


CASES: list[tuple[str, Callable[[], str]]] = [
    ("rag_citations", _rag_citations),
    ("tool_loop", _tool_loop),
    ("web_rank_conflict", _web_rank_and_conflict),
    ("external_react", _external_react),
    ("failure_injection", _failure_injection),
    ("permission_gate", _permission_gate),
]


def run_evals() -> list[EvalResult]:
    out = []
    for name, case in CASES:
        started = time.perf_counter()
        try:
            detail = case()
            out.append(EvalResult(name, True, detail, int((time.perf_counter() - started) * 1000)))
        except Exception as exc:  # noqa: BLE001
            out.append(
                EvalResult(
                    name,
                    False,
                    f"{type(exc).__name__}: {exc}",
                    int((time.perf_counter() - started) * 1000),
                )
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Xinghuan Chat deterministic evals")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    results = run_evals()
    if args.json:
        print(json.dumps([r.public() for r in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"{'PASS' if result.ok else 'FAIL'}  {result.name}  ({result.duration_ms} ms)")
            if result.detail:
                print(f"      {result.detail}")
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
