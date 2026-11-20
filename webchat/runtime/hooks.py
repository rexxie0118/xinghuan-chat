"""Synchronous lifecycle hooks for the WebChat agent runtime.

Hooks are deliberately small and in-process. They are for trusted built-in
extensions, not for tenant-supplied code. Untrusted tools belong behind MCP or
a sandboxed Agent.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

LOG = logging.getLogger("webchat.runtime")

Handler = Callable[[str, dict[str, Any], "HookContext"], None]


@dataclass
class HookContext:
    """What a hook may read or register against."""

    settings: Any
    kernel: Any
    data: dict[str, Any] = field(default_factory=dict)

    def on(self, event: str, handler: Handler) -> None:
        self.kernel.hooks.on(event, handler)


class RuntimePlugin(Protocol):
    name: str

    def setup(self, ctx: HookContext) -> None: ...


class HookBus:
    """Ordered, failure-isolated event bus.

    A hook error is logged and emitted as a warning instead of taking down the
    chat. Security/approval hooks that must fail closed should return an
    explicit decision rather than relying on exceptions.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def on(self, event: str, handler: Handler) -> None:
        self._handlers[event].append(handler)

    def emit(self, event: str, payload: dict[str, Any], ctx: HookContext) -> dict[str, Any]:
        for handler in list(self._handlers.get(event, [])):
            try:
                handler(event, payload, ctx)
            except Exception as exc:  # noqa: BLE001 - hooks must not break chat
                LOG.warning("hook %s failed: %s", event, exc)
                payload.setdefault("hook_errors", []).append(
                    f"{type(exc).__name__}: {exc}"
                )
        return payload

    def clear(self) -> None:
        self._handlers.clear()
