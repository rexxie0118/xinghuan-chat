"""External Agent client with an optional MCP/ReAct tool loop.

The external service still owns reasoning. WebChat supplies tool schemas,
executes requested tools through the local ToolRegistry (MCP/plugin/connector),
and sends observations back. If the service only returns ``answer``, it behaves
like the old one-shot external Agent.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .runtime.cancel import CancellationToken
from .runtime.failures import CircuitBreaker, RetryPolicy
from .tools import ToolError, ToolRegistry, ToolSpec


class ExternalAgentClient:
    def __init__(
        self,
        settings: Any,
        tools: ToolRegistry | None = None,
        *,
        breaker: CircuitBreaker | None = None,
        poster=None,
        max_steps: int = 4,
    ) -> None:
        self.settings = settings
        self.tools = tools
        self.breaker = breaker or CircuitBreaker("external-agent")
        self.poster = poster or httpx.post
        self.max_steps = max(1, min(int(max_steps), 8))

    def _tool_schemas(self) -> list[dict[str, Any]]:
        if self.tools is None:
            return []
        out = []
        for spec in self.tools.list_tools():
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description[:1000],
                        "parameters": spec.input_schema or {"type": "object"},
                    },
                }
            )
        return out

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if getattr(self.settings, "agent_token", None):
            headers["Authorization"] = f"Bearer {self.settings.agent_token}"

        def request() -> dict[str, Any]:
            resp = self.poster(
                str(self.settings.agent_url).rstrip("/") + "/chat",
                json=body,
                headers=headers,
                timeout=float(self.settings.agent_timeout),
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError("外部 Agent 返回不是 JSON object")
            return data

        return RetryPolicy(attempts=2, base_delay=0.3, max_delay=2).run(
            request, breaker=self.breaker
        )

    @staticmethod
    def _calls(data: dict[str, Any]) -> list[dict[str, Any]]:
        calls = data.get("tool_calls")
        if isinstance(calls, list):
            return [
                {
                    "id": str(call.get("id") or ""),
                    "name": str((call.get("function") or {}).get("name") or call.get("name") or ""),
                    "arguments": (call.get("function") or {}).get("arguments")
                    or call.get("arguments")
                    or {},
                }
                for call in calls
                if isinstance(call, dict)
            ]
        action = data.get("action") or data.get("tool")
        if action:
            return [
                {
                    "id": str(data.get("id") or ""),
                    "name": str(action),
                    "arguments": data.get("arguments") or {},
                }
            ]
        return []

    def run(
        self,
        *,
        question: str,
        mode: str,
        user: dict[str, Any],
        conversation: dict[str, Any],
        history: list[dict[str, str]],
        retrieval: dict[str, Any],
        kb: Any,
        cancel_token: CancellationToken | None = None,
    ) -> dict[str, Any] | None:
        if not getattr(self.settings, "agent_url", None):
            return None
        if self.tools is not None and not self.tools.list_tools():
            tools = []
        else:
            tools = self._tool_schemas()
        hits = []
        for i, hit in enumerate(retrieval.get("hits", [])[:6], 1):
            hits.append(
                {
                    "index": i,
                    "chunk_id": hit["chunk_id"],
                    "doc_title": hit["doc_title"],
                    "heading_path": hit.get("heading_path", ""),
                    "text": hit.get("text", "")[:1400],
                    "score": hit.get("score", 0),
                }
            )
        messages = history[-8:] + [{"role": "user", "content": question}]
        observations: list[dict[str, Any]] = []
        trace: list[dict[str, Any]] = []
        tools_used: list[str] = []
        last_answer = ""
        for step in range(self.max_steps):
            if cancel_token is not None:
                cancel_token.check()
            body = {
                "react": bool(tools),
                "step": step,
                "tools": tools,
                "user": {"id": user.get("id"), "name": user.get("display_name")},
                "conversation_id": conversation.get("id"),
                "knowledge_base": {
                    "id": kb.info.id,
                    "name": kb.info.name,
                    "department": kb.info.department,
                },
                "mode": mode,
                "messages": messages,
                "context": {
                    "question": question,
                    "hits": hits,
                    "graph": retrieval.get("graph", {}),
                    "memories": retrieval.get("memories", [])[:4],
                },
                "observations": observations,
            }
            data = self._post(body)
            answer = str(data.get("answer") or "").strip()
            if answer:
                last_answer = answer
            calls = self._calls(data)
            if not calls:
                if answer:
                    return {
                        "answer": answer,
                        "citations": data.get("citations") or [],
                        "trace": trace,
                        "tools_used": tools_used,
                        "suggestions": data.get("suggestions") or [],
                        "confidence": data.get("confidence", 0),
                        "steps": step + 1,
                    }
                return None if not last_answer else {
                    "answer": last_answer,
                    "citations": data.get("citations") or [],
                    "trace": trace,
                    "tools_used": tools_used,
                    "suggestions": data.get("suggestions") or [],
                    "confidence": data.get("confidence", 0),
                    "steps": step + 1,
                }
            for call in calls:
                name = call["name"]
                arguments = call["arguments"]
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments or "{}")
                    except ValueError:
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                started = time.perf_counter()
                if self.tools is None:
                    result = None
                    error = "本地没有 ToolRegistry"
                else:
                    try:
                        result = self.tools.call(name, arguments)
                        error = ""
                    except ToolError as exc:
                        result = None
                        error = str(exc)
                duration = int((time.perf_counter() - started) * 1000)
                ok = bool(result and result.ok)
                observation = {
                    "tool": name,
                    "ok": ok,
                    "content": (result.content if result else error)[:6000],
                    "sources": result.sources if result else [],
                }
                observations.append(observation)
                if result is not None:
                    tools_used.append(name)
                trace.append(
                    {
                        "title": "外部 Agent 调用工具",
                        "detail": f"{name} · {'ok' if ok else 'error'} · {duration} ms",
                        "kind": "tool",
                        "status": "done" if ok else "error",
                        "duration_ms": duration,
                        "data": {"tool": name},
                    }
                )
            trace.append(
                {
                    "title": "外部 Agent 观察",
                    "detail": f"收到 {len(calls)} 个工具结果，继续推理",
                    "kind": "think",
                    "status": "done",
                    "duration_ms": 0,
                    "data": {},
                }
            )
        return {
            "answer": last_answer or "外部 Agent 已达到工具步数上限，未得到最终回答。",
            "citations": [],
            "trace": trace,
            "tools_used": tools_used,
            "suggestions": [],
            "confidence": 0,
            "steps": self.max_steps,
        }
