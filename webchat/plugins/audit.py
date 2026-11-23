"""Small built-in plugin used as a manifest example."""

from __future__ import annotations

name = "audit"


class AuditPlugin:
    def setup(self, ctx) -> None:
        ctx.on("agent_end", _on_agent_end)
        ctx.register_tool(
            "ping",
            description="Health check for the plugin runtime",
            input_schema={"type": "object", "properties": {}},
            handler=lambda _args: {"content": "pong"},
        )


def _on_agent_end(event, payload, ctx) -> None:
    result = payload.get("result")
    if result is not None:
        ctx.data["last_answer_mode"] = getattr(result, "meta", {}).get("answer_mode", "")


plugin = AuditPlugin()
