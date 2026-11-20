"""Apple `container` sandbox runner.

This adapter runs an imported Agent script in a disposable Apple Container
(https://github.com/apple/container), one container per execution. The
container gets only:

  * a host input directory mounted at /agent/in (read-write)
  * a host output directory mounted at /agent/out
  * a tmpfs /tmp
  * a read-only root filesystem, no capabilities, and no network by default

The container is removed after the run. Generated files are copied back by the
host-side artifact store before cleanup. For persistent per-user containers,
use a separate ContainerRuntime manager; this class only implements the
existing one-shot SandboxRunner contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from ..artifacts import LocalArtifactStore
from . import RunRequest, RunResult
from .local import MAX_OUTPUT_CHARS, _truncate

RUNTIME_COMMANDS = {
    "python": ["python"],
    "bash": ["bash"],
    "node": ["node"],
}


class AppleContainerRunner:
    name = "apple-container"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.artifacts = LocalArtifactStore(settings.data_dir)
        self.binary = str(
            getattr(settings, "apple_container_bin", None) or "container"
        )
        self.image = str(
            getattr(settings, "apple_container_image", None) or "python:3.12-slim"
        )
        self.cpus = str(getattr(settings, "apple_container_cpus", 2) or 2)
        self.memory = str(getattr(settings, "apple_container_memory", None) or "2G")
        self.network = str(getattr(settings, "apple_container_network", None) or "default")

    def _binary(self) -> str | None:
        if os.path.sep in self.binary:
            path = Path(self.binary).expanduser()
            return str(path) if path.exists() else None
        return shutil.which(self.binary)

    def _base_command(self, *, name: str, input_dir: Path, output_dir: Path) -> list[str]:
        return [
            self._binary() or self.binary,
            "run",
            "--rm",
            "--name",
            name,
            "--init",
            "--read-only",
            "--cap-drop",
            "ALL",
            *(["--network", self.network] if self.network.lower() != "default" else []),
            "--cpus",
            self.cpus,
            "--memory",
            self.memory,
            "--tmpfs",
            "/tmp:size=64M",
            "--volume",
            f"{input_dir}:/agent/in",
            "--volume",
            f"{output_dir}:/agent/out",
            "--workdir",
            "/agent/in",
            "--env",
            "AGENT_INPUT_DIR=/agent/in",
            "--env",
            "AGENT_OUTPUT_DIR=/agent/out",
            "--env",
            "HOME=/tmp",
            "--env",
            "LANG=C.UTF-8",
            "--env",
            "LC_ALL=C.UTF-8",
            "--env",
            "PYTHONUNBUFFERED=1",
            "--env",
            "PYTHONIOENCODING=utf-8",
            "--env",
            "NODE_NO_WARNINGS=1",
            "-i",
            self.image,
        ]

    def _command(self, request: RunRequest, *, name: str, input_dir: Path, output_dir: Path) -> list[str]:
        command = self._base_command(name=name, input_dir=input_dir, output_dir=output_dir)
        for key, value in request.env.items():
            command.extend(["--env", f"{key}={value}"])
        runtime = RUNTIME_COMMANDS.get(request.runtime)
        if runtime is None:
            raise ValueError(f"不支持的 runtime：{request.runtime}")
        command.extend(runtime)
        if request.runtime == "python":
            command.extend(["-I", "-B"])
        if request.runtime == "node":
            command.append("--no-warnings")
        command.append(request.entrypoint)
        command.append(request.question[:4000])
        return command

    def run(self, request: RunRequest) -> RunResult:
        if not request.entrypoint:
            return RunResult(
                ran=False,
                reason="该 Agent/Skill 没有配置入口脚本",
                runner=self.name,
                run_id=request.run_id,
            )
        if not request.allow_exec:
            return RunResult(
                ran=False,
                reason="服务端已禁用脚本执行（WEBCHAT_ALLOW_SCRIPT_EXEC=0）",
                runner=self.name,
                run_id=request.run_id,
            )
        if request.runtime not in RUNTIME_COMMANDS:
            return RunResult(
                ran=False,
                reason=f"不支持的 runtime：{request.runtime}",
                runner=self.name,
                run_id=request.run_id,
            )
        if self.network.lower() == "none":
            return RunResult(
                ran=False,
                reason=(
                    "Apple Container 目前没有 --network none；"
                    "请使用 APPLE_CONTAINER_NETWORK=default 并在宿主机配置出站防火墙"
                ),
                runner=self.name,
                run_id=request.run_id,
            )
        binary = self._binary()
        if not binary:
            return RunResult(
                ran=False,
                reason=(
                    "当前环境找不到 Apple Container CLI；请安装 "
                    "https://github.com/apple/container 后执行 `container system start`"
                ),
                runner=self.name,
                run_id=request.run_id,
            )

        root = Path(tempfile.mkdtemp(prefix=f"wc_apple_{request.run_id}_"))
        input_dir = root / "in"
        output_dir = root / "out"
        input_dir.mkdir()
        output_dir.mkdir()
        safe_run_id = "".join(
            ch if ch.isalnum() or ch == "-" else "-" for ch in request.run_id.lower()
        ).strip("-")
        name = f"xh-run-{safe_run_id}"[:63]
        started = time.perf_counter()
        proc: subprocess.Popen[str] | None = None
        try:
            for item in request.files:
                target = (input_dir / item["name"]).resolve()
                if not target.is_relative_to(input_dir.resolve()):
                    raise ValueError(f"非法输入路径：{item['name']}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item["content"], encoding="utf-8")
            initial_paths = {
                p.relative_to(input_dir).as_posix()
                for p in input_dir.rglob("*")
                if p.is_file()
            }
            entry = (input_dir / request.entrypoint).resolve()
            if not entry.is_relative_to(input_dir.resolve()) or not entry.exists():
                return RunResult(
                    ran=False,
                    reason=f"入口脚本不存在：{request.entrypoint}",
                    runner=self.name,
                    run_id=request.run_id,
                )
            command = self._command(
                request, name=name, input_dir=input_dir.resolve(), output_dir=output_dir.resolve()
            )
            payload = _payload(request)
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(payload, timeout=request.timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_container(binary, name)
                stdout, stderr = proc.communicate()
            duration = int((time.perf_counter() - started) * 1000)
            exit_code = proc.returncode
            artifacts = self._persist_artifacts(
                run_id=request.run_id,
                input_dir=input_dir,
                output_dir=output_dir,
                initial_paths=initial_paths,
            )
            return RunResult(
                ran=True,
                command=f"container run {self.image} {request.runtime} {request.entrypoint}",
                exit_code=exit_code,
                stdout=_truncate(stdout or ""),
                stderr=_truncate(stderr or ""),
                timed_out=timed_out,
                duration_ms=duration,
                artifacts=artifacts,
                runner=self.name,
                run_id=request.run_id,
            )
        except Exception as exc:  # noqa: BLE001 - a failed run is a trace, not a crash
            return RunResult(
                ran=False,
                reason=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.perf_counter() - started) * 1000),
                runner=self.name,
                run_id=request.run_id,
            )
        finally:
            if proc is not None and proc.poll() is None:
                _kill_container(binary or self.binary, name)
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(root, ignore_errors=True)

    def _persist_artifacts(
        self,
        *,
        run_id: str,
        input_dir: Path,
        output_dir: Path,
        initial_paths: set[str],
    ) -> list[dict[str, Any]]:
        try:
            return self.artifacts.persist(
                run_id=run_id,
                input_dir=input_dir,
                output_dir=output_dir,
                initial_paths=initial_paths,
            )
        except Exception:  # noqa: BLE001
            return []


def _payload(request: RunRequest) -> str:
    import json

    return json.dumps(
        {
            "question": request.question,
            "agent": {
                "name": request.agent_name,
                "description": request.agent_description,
                "markdown": request.agent_markdown,
                "entrypoint": request.entrypoint,
            },
            "context": request.context,
        },
        ensure_ascii=False,
    )


def _kill_container(binary: str, name: str) -> None:
    for command in (
        [binary, "stop", "--time", "2", name],
        [binary, "kill", name],
        [binary, "delete", "--force", name],
    ):
        try:
            subprocess.run(command, capture_output=True, timeout=8, check=False)
        except Exception:  # noqa: BLE001
            pass
