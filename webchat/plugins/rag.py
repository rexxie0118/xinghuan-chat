"""RAG answer adapter exposed as an internal capability."""

from __future__ import annotations

name = "rag"


class RagPlugin:
    def setup(self, ctx) -> None:
        ctx.register_capability("rag", "answer", answer, priority=100)


def answer(payload):
    return payload["kb"].answer(
        payload["question"],
        payload["result"],
        history=payload.get("history"),
        memories=payload.get("memories") or [],
    )


plugin = RagPlugin()
