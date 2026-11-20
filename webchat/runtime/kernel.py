"""The thin agent kernel: lifecycle hooks around the existing AgentRunner.

This is intentionally a seam, not a rewrite. The current RAG/ImportedAgent
engine remains in AgentRunner; the kernel will later host the tool loop,
context budget and failure policy as built-in plugins.
"""

from __future__ import annotations

import logging
from typing import Any

from .hooks import HookBus, HookContext, RuntimePlugin

LOG = logging.getLogger("webchat.runtime")


class AgentKernel:
    def __init__(self, settings: Any, runner: Any) -> None:
        self.settings = settings
        self.runner = runner
        self.hooks = HookBus()
        self.context = HookContext(settings=settings, kernel=self)
        runner.attach_hooks(self.hooks, self.context)

    def use(self, plugin: RuntimePlugin) -> "AgentKernel":
        plugin.setup(self.context)
        return self

    def run(self, **kwargs: Any):
        self.hooks.emit(
            "agent_start",
            {
                "question": kwargs.get("question", ""),
                "mode": kwargs.get("mode", "rag"),
                "user_id": (kwargs.get("user") or {}).get("id", ""),
                "conversation_id": (kwargs.get("conversation") or {}).get("id", ""),
            },
            self.context,
        )
        try:
            result = self.runner.run(**kwargs)
        except Exception as exc:
            self.hooks.emit(
                "agent_error",
                {"error": f"{type(exc).__name__}: {exc}"},
                self.context,
            )
            raise
        self.hooks.emit(
            "agent_end",
            {
                "result": result,
                "answer_mode": (result.meta or {}).get("answer_mode", ""),
                "trace_steps": len(result.trace or []),
            },
            self.context,
        )
        return result
