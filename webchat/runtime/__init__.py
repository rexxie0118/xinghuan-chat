"""Small agent runtime seams.

The kernel owns lifecycle hooks; concrete engines (RAG, imported Agent, future
tool loop) stay behind the existing AgentRunner/SandboxRunner interfaces.
"""

from .hooks import HookBus, HookContext, RuntimePlugin
from .kernel import AgentKernel

__all__ = ["AgentKernel", "HookBus", "HookContext", "RuntimePlugin"]
