"""MCP tool-provider adapter for the internal plugin host."""

from __future__ import annotations

name = "mcp"


class MCPPlugin:
    def setup(self, ctx) -> None:
        ctx.register_capability("tool_provider", "mcp", provider, priority=100)


def provider(payload):
    return payload.get("tools")


plugin = MCPPlugin()
