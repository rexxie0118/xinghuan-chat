"""Sandbox execution contract.

Agent scripts must never be wired directly into the web process. The orchestrator
talks to a ``SandboxRunner``; today only ``local`` is implemented, but a Docker /
Kubernetes / gVisor runner can be registered without touching the Agent or chat
layers.

Adding a runner::

    from webchat.sandbox import register_runner

    register_runner("docker", lambda settings: DockerSandboxRunner(settings))
    # WEBCHAT_SANDBOX=docker

The contract also carries an input/output workspace and an ``artifacts`` list,
so a future runner can persist generated files without changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    files: list[dict[str, str]]
    entrypoint: str
    runtime: str
    timeout: float
    question: str
    context: dict[str, Any]
    agent_name: str
    agent_description: str
    agent_markdown: str
    env: dict[str, str] = field(default_factory=dict)
    allow_exec: bool = True


@dataclass
class RunResult:
    ran: bool
    reason: str = ""
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    runner: str = "local"
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "reason": self.reason,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "duration_ms": self.duration_ms,
            "artifacts": self.artifacts,
            "runner": self.runner,
            "run_id": self.run_id,
        }


class SandboxRunner(Protocol):
    name: str

    def run(self, request: RunRequest) -> RunResult: ...


RunnerFactory = Callable[[Any], SandboxRunner]
_FACTORIES: dict[str, RunnerFactory] = {}


def register_runner(name: str, factory: RunnerFactory) -> None:
    _FACTORIES[name.strip().lower()] = factory


def available_runners() -> list[str]:
    return sorted(_FACTORIES)


def build_sandbox_runner(settings: Any) -> SandboxRunner:
    name = str(getattr(settings, "sandbox", "local") or "local").strip().lower()
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"未知沙箱 runner: {name!r}；已注册：{', '.join(available_runners()) or '无'}"
        )
    return factory(settings)


def _register_builtin() -> None:
    from .local import LocalSubprocessRunner

    register_runner("local", lambda settings: LocalSubprocessRunner(settings))


_register_builtin()
