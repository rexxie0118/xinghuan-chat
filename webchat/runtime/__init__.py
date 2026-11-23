"""Small agent runtime seams.

The kernel owns lifecycle hooks; concrete engines (RAG, imported Agent, future
tool loop) stay behind the existing AgentRunner/SandboxRunner interfaces.
"""

from .cancel import ActiveRuns, CancellationToken
from .failures import CircuitBreaker, FailureClass, RetryPolicy, RunCancelled
from .hooks import HookBus, HookContext, RuntimePlugin
from .kernel import AgentKernel

__all__ = [
    "ActiveRuns",
    "AgentKernel",
    "CancellationToken",
    "CircuitBreaker",
    "FailureClass",
    "HookBus",
    "HookContext",
    "RetryPolicy",
    "RunCancelled",
    "RuntimePlugin",
]
