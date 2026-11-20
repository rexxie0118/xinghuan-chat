"""Tests for the agent runtime hook bus and kernel seam."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from webchat.runtime import AgentKernel, HookBus, HookContext, RuntimePlugin


class FakeRunner:
    def __init__(self):
        self.hooks = None
        self.context = None

    def attach_hooks(self, hooks, context):
        self.hooks = hooks
        self.context = context

    def run(self, **kwargs):
        return SimpleNamespace(
            meta={"answer_mode": "fake"},
            trace=[{"title": "fake"}],
            answer="ok",
        )


class RuntimeTest(unittest.TestCase):
    def test_hook_bus_isolates_errors_and_preserves_order(self):
        bus = HookBus()
        calls = []
        ctx = HookContext(settings=None, kernel=SimpleNamespace(hooks=bus))

        def boom(event, payload, _ctx):
            calls.append("boom")
            raise RuntimeError("hook failed")

        bus.on("x", lambda event, payload, _ctx: calls.append("first"))
        bus.on("x", boom)
        bus.on("x", lambda event, payload, _ctx: calls.append("last"))
        payload = bus.emit("x", {}, ctx)
        self.assertEqual(calls, ["first", "boom", "last"])
        self.assertEqual(len(payload["hook_errors"]), 1)

    def test_kernel_loads_plugin_and_emits_lifecycle(self):
        class Plugin:
            name = "observer"

            def setup(self, ctx):
                ctx.on("agent_start", lambda event, payload, _ctx: payload.setdefault("started", True))
                ctx.on("agent_end", lambda event, payload, _ctx: payload.setdefault("ended", True))

        runner = FakeRunner()
        kernel = AgentKernel(SimpleNamespace(), runner)
        kernel.use(Plugin())
        result = kernel.run(question="hi", mode="rag", user={"id": "u"}, conversation={"id": "c"})
        self.assertEqual(result.answer, "ok")
        self.assertIsNotNone(runner.hooks)
        self.assertEqual(runner.context.kernel, kernel)


if __name__ == "__main__":
    unittest.main()
