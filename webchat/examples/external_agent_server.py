"""Reference external Agent service (put this in your Agent repo).

The web app never imports Agent code. It POSTs to ``/chat`` with grounded
context and expects a small JSON contract back. This file is a runnable stub:

    cd webchat/examples
    ../../.venv/bin/python external_agent_server.py   # listens on :8790

Then start 星环 Chat with:

    WEBCHAT_AGENT_URL=http://127.0.0.1:8790 python -m webchat

Replace ``answer()`` with your real agent loop (LangGraph, ADK, ReAct, ...).
The context already contains RAG hits, a 2-hop graph and the recent messages.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel


class ChatIn(BaseModel):
    user: dict[str, Any] = {}
    conversation_id: str = ""
    knowledge_base: dict[str, Any] = {}
    mode: str = "agent"
    messages: list[dict[str, str]] = []
    context: dict[str, Any] = {}


app = FastAPI(title="External Agent Demo")


def answer(payload: ChatIn) -> dict[str, Any]:
    question = (payload.messages[-1]["content"] if payload.messages else "").strip()
    hits = (payload.context or {}).get("hits") or []
    graph = (payload.context or {}).get("graph") or {}
    if not hits:
        return {
            "answer": "（外部 Agent）这次没有拿到知识上下文。请先确认 Web 应用的知识库已摄取文档。",
            "citations": [],
            "trace": [{"title": "读取上下文", "detail": "hits=0", "status": "done"}],
            "suggestions": ["北极星项目的负责人是谁？"],
        }

    # A transparent demo policy: quote the best grounded passage. A real Agent
    # would call tools / an LLM here and return its own trace.
    top = hits[:2]
    lines = ["（外部 Agent 演示）我拿到了 Web 应用传来的证据："]
    citations = []
    for index, hit in enumerate(top, 1):
        text = " ".join((hit.get("text") or "").split())
        lines.append(f"- {text[:220]} [{index}]")
        citations.append(index)
    if graph.get("nodes"):
        names = "、".join(n.get("name", "") for n in graph["nodes"][:4])
        lines.append(f"\n关联实体：{names}。")
    lines.append("\n这段回答由独立仓库里的 Agent 服务生成，Web 应用只负责持久化与展示。")

    return {
        "answer": "\n".join(lines),
        "citations": citations,
        "confidence": 0.66,
        "trace": [
            {
                "title": "接收上下文",
                "detail": (
                    f"kb={payload.knowledge_base.get('id', '?')} · "
                    f"messages={len(payload.messages)} · hits={len(hits)} · "
                    f"entities={len(graph.get('nodes', []))}"
                ),
                "status": "done",
            },
            {
                "title": "选择证据",
                "detail": f"引用前 {len(top)} 个片段，问题：{question[:40]}",
                "status": "done",
            },
            {
                "title": "生成回答",
                "detail": "由外部 Agent 服务生成（示例策略：抽取式）",
                "status": "done",
            },
        ],
        "suggestions": ["统一登录服务依赖什么？", "多用户的数据是怎么隔离的？"],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "external-agent-demo"}


@app.post("/chat")
def chat(payload: ChatIn) -> dict[str, Any]:
    return answer(payload)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("AGENT_PORT", "8790")))
