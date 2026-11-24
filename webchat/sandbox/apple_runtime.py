"""Persistent Apple Container runtime manager.

One-shot Agent execution already lives in ``apple.py``. This module manages
long-lived per-user / per-session containers and their named volumes:

  * ensure volume
  * create / start / stop / delete container
  * inspect / list managed containers
  * deterministic names from a runtime key

It deliberately does not own session state or idle policy. Those belong in the
control plane database; this file only talks to the ``container`` CLI.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ContainerRuntimeError(RuntimeError):
    pass


@dataclass
class RuntimeInfo:
    key: str
    name: str
    volume: str
    image: str
    state: str = "missing"
    container_id: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "volume": self.volume,
            "image": self.image,
            "state": self.state,
            "container_id": self.container_id,
        }


class AppleContainerRuntime:
    name = "apple-container-runtime"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.binary = str(getattr(settings, "apple_container_bin", None) or "container")
        self.image = str(
            getattr(settings, "apple_container_image", None) or "python:3.12-slim"
        )
        self.cpus = str(getattr(settings, "apple_container_cpus", 2) or 2)
        self.memory = str(getattr(settings, "apple_container_memory", None) or "2G")
        self.network = str(getattr(settings, "apple_container_network", None) or "default")

    # ------------------------------------------------------------ CLI helpers
    def available(self) -> bool:
        if "/" in self.binary:
            return Path(self.binary).exists()
        return shutil.which(self.binary) is not None

    def _run(self, args: list[str], *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        if not self.available():
            raise ContainerRuntimeError(
                "Apple Container CLI 不存在；请安装 https://github.com/apple/container"
            )
        try:
            proc = subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ContainerRuntimeError(f"container {' '.join(args)} 超时") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-600:]
            raise ContainerRuntimeError(
                f"container {' '.join(args)} 失败（exit={proc.returncode}）：{detail}"
            )
        return proc

    @staticmethod
    def _key_hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def names_for(cls, key: str) -> tuple[str, str]:
        digest = cls._key_hash(key)
        return f"xh-rt-{digest}", f"xh-vol-{digest}"

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", value).strip("-")
        return (cleaned or "xh-runtime")[:63]

    def _volume_exists(self, name: str) -> bool:
        try:
            self._run(["volume", "inspect", name], timeout=20)
            return True
        except ContainerRuntimeError:
            return False

    def ensure_volume(self, name: str) -> None:
        if not self._volume_exists(name):
            self._run(["volume", "create", name], timeout=30)

    def _all_containers(self) -> list[dict[str, Any]]:
        proc = self._run(["list", "--all", "--format", "json"], timeout=30)
        try:
            data = json.loads(proc.stdout or "[]")
        except ValueError as exc:
            raise ContainerRuntimeError("container list 返回了非 JSON") from exc
        return data if isinstance(data, list) else []

    @staticmethod
    def _container_info(item: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
        config = item.get("configuration") or {}
        container_id = str(item.get("id") or config.get("id") or "")
        state = str((item.get("status") or {}).get("state") or "unknown")
        labels = config.get("labels") or item.get("labels") or {}
        return container_id, state, labels

    def info(self, key: str) -> RuntimeInfo:
        name, volume = self.names_for(key)
        digest = self._key_hash(key)
        found: dict[str, Any] | None = None
        for item in self._all_containers():
            container_id, _state, labels = self._container_info(item)
            if container_id == name or labels.get("xh.key") == digest:
                found = item
                break
        if found is None:
            return RuntimeInfo(key=key, name=name, volume=volume, image=self.image)
        container_id, state, _ = self._container_info(found)
        return RuntimeInfo(
            key=key,
            name=name,
            volume=volume,
            image=self.image,
            state=state,
            container_id=container_id or name,
        )

    def list_managed(self) -> list[RuntimeInfo]:
        out: list[RuntimeInfo] = []
        for item in self._all_containers():
            container_id, state, labels = self._container_info(item)
            if labels.get("xh.managed") != "true":
                continue
            key_hash = labels.get("xh.key") or ""
            out.append(
                RuntimeInfo(
                    key=key_hash,
                    name=container_id,
                    volume=labels.get("xh.volume") or "",
                    image=self.image,
                    state=state,
                    container_id=container_id,
                )
            )
        return out

    def ensure(
        self,
        key: str,
        *,
        image: str | None = None,
        command: list[str] | None = None,
        create_args: list[str] | None = None,
    ) -> RuntimeInfo:
        name, volume = self.names_for(key)
        image = image or self.image
        command = command or ["sleep", "infinity"]
        current = self.info(key)
        if current.state == "running":
            return current
        self.ensure_volume(volume)
        if current.state == "missing":
            args = [
                "create",
                "--name",
                name,
                "--label",
                "xh.managed=true",
                "--label",
                f"xh.key={self._key_hash(key)}",
                "--label",
                f"xh.volume={volume}",
                "--volume",
                f"{volume}:/workspace",
                "--cpus",
                self.cpus,
                "--memory",
                self.memory,
            ]
            if create_args:
                args += [str(item) for item in create_args]
            if self.network and self.network.lower() != "default":
                args += ["--network", self.network]
            if self.network.lower() == "none":
                raise ContainerRuntimeError(
                    "Apple Container 目前没有 --network none；请用 default + 宿主防火墙"
                )
            args.append(image)
            args += command
            self._run(args, timeout=90)
        self._run(["start", name], timeout=60)
        return self.info(key)

    def start(self, key: str) -> RuntimeInfo:
        return self.ensure(key)

    def stop(self, key: str) -> RuntimeInfo:
        current = self.info(key)
        if current.state in {"missing", "stopped"}:
            return current
        self._run(["stop", "--time", "5", current.container_id or current.name], timeout=30)
        return self.info(key)

    def delete(self, key: str, *, delete_volume: bool = False) -> bool:
        current = self.info(key)
        if current.state != "missing":
            self._run(["delete", "--force", current.container_id or current.name], timeout=60)
        if delete_volume and self._volume_exists(current.volume):
            self._run(["volume", "delete", current.volume], timeout=60)
        return True

    def exec(self, key: str, command: list[str], *, timeout: float = 30) -> subprocess.CompletedProcess[str]:
        current = self.info(key)
        if current.state != "running":
            raise ContainerRuntimeError(f"runtime {key!r} 未运行")
        return self._run(["exec", current.container_id or current.name, *command], timeout=timeout)
