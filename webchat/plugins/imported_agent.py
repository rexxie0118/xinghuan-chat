"""Imported Agent execution adapter.

The heavy lifting stays in AgentRunner; this capability is the plugin seam used
to route imported Agent/Skill execution through the internal plugin host.
"""

from __future__ import annotations

name = "imported_agent"


class ImportedAgentPlugin:
    def setup(self, ctx) -> None:
        ctx.register_capability("agent", "imported", run_imported, priority=100)


def run_imported(payload):
    return payload["runner"]._run_imported_impl(**payload["kwargs"])


plugin = ImportedAgentPlugin()
