"""Local development runner.

This is the current demo implementation: a time-boxed subprocess in a temporary
directory. It is *not* a security boundary. It exists behind ``SandboxRunner`` so
production can swap in Docker/Kubernetes without changing Agent orchestration.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..artifacts import LocalArtifactStore
from . import RunRequest, RunResult

MAX_OUTPUT_CHARS = 20_000
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACTS_TOTAL = 10 * 1024 * 1024


def _safe_limit() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except Exception:
        pass


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n… (输出已截断)"


class LocalSubprocessRunner:
    name = "local"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.artifacts = LocalArtifactStore(settings.data_dir)

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
        if request.runtime not in {"python", "bash", "node"}:
            return RunResult(
                ran=False,
                reason=f"不支持的 runtime：{request.runtime}",
                runner=self.name,
                run_id=request.run_id,
            )

        root = Path(tempfile.mkdtemp(prefix=f"wc_run_{request.run_id}_"))
        input_dir = root / "in"
        output_dir = root / "out"
        input_dir.mkdir()
        output_dir.mkdir()
        started = time.perf_counter()
        proc = None
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
            if not entry.is_relative_to(input_dir.resolve()):
                raise ValueError("入口脚本路径越界")
            if not entry.exists():
                return RunResult(
                    ran=False,
                    reason=f"入口脚本不存在：{request.entrypoint}",
                    runner=self.name,
                    run_id=request.run_id,
                )

            if request.runtime == "python":
                command = [sys.executable, "-I", "-B", request.entrypoint, request.question[:4000]]
            elif request.runtime == "bash":
                command = ["/bin/bash", "--noprofile", "--norc", request.entrypoint, request.question[:4000]]
            else:
                node = shutil.which("node")
                if not node:
                    return RunResult(
                        ran=False,
                        reason="当前环境没有 node",
                        runner=self.name,
                        run_id=request.run_id,
                    )
                command = [node, "--no-warnings", request.entrypoint, request.question[:4000]]

            payload = json.dumps(
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
            env = {
                "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(root),
                "TMPDIR": str(root),
                "AGENT_INPUT_DIR": str(input_dir),
                "AGENT_OUTPUT_DIR": str(output_dir),
                "AGENT_RUN_ID": request.run_id,
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "NODE_NO_WARNINGS": "1",
            }
            env.update({str(k): str(v) for k, v in request.env.items()})
            proc = subprocess.Popen(
                command,
                cwd=str(input_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                start_new_session=True,
                preexec_fn=_safe_limit if os.name == "posix" else None,
            )
            try:
                stdout, stderr = proc.communicate(payload, timeout=request.timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
                stdout, stderr = proc.communicate()
            duration = int((time.perf_counter() - started) * 1000)
            return RunResult(
                ran=True,
                command=f"{request.runtime} {request.entrypoint}",
                exit_code=proc.returncode,
                stdout=_truncate(stdout or ""),
                stderr=_truncate(stderr or ""),
                timed_out=timed_out,
                duration_ms=duration,
                artifacts=self._persist_artifacts(
                    run_id=request.run_id,
                    input_dir=input_dir,
                    output_dir=output_dir,
                    initial_paths=initial_paths,
                ),
                runner=self.name,
                run_id=request.run_id,
            )
        except Exception as exc:  # noqa: BLE001 - execution failure is a trace, not a crash
            return RunResult(
                ran=False,
                reason=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.perf_counter() - started) * 1000),
                runner=self.name,
                run_id=request.run_id,
            )
        finally:
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
        except Exception:
            return []

    @staticmethod
    def _collect_artifacts(output_dir: Path) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        total = 0
        if not output_dir.exists():
            return artifacts
        for root, dirs, names in os.walk(output_dir, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
            for name in sorted(names):
                path = Path(root) / name
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                if size > MAX_ARTIFACT_BYTES or total + size > MAX_ARTIFACTS_TOTAL:
                    return artifacts
                data = path.read_bytes()
                total += size
                artifacts.append(
                    {
                        "name": path.relative_to(output_dir).as_posix(),
                        "size": size,
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
                if len(artifacts) >= 50:
                    return artifacts
        return artifacts
