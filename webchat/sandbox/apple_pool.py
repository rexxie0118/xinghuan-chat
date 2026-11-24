"""Warm pool and owner quotas for Apple Containers.

The pool owns idle, trusted runtime containers. Callers acquire a lease, run
commands with ``container exec``, and return it. It is deliberately separate
from the one-shot ``AppleContainerRunner`` until execution is migrated.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .apple_runtime import AppleContainerRuntime, ContainerRuntimeError, RuntimeInfo

POOL_CREATE_ARGS = [
    "--read-only",
    "--cap-drop",
    "ALL",
    "--tmpfs",
    "/tmp:size=64M",
]


class PoolExhausted(RuntimeError):
    pass


@dataclass
class PoolLease:
    owner: str
    info: RuntimeInfo
    key: str
    acquired_at: float
    broken: bool = False


class AppleContainerPool:
    def __init__(
        self,
        settings: Any,
        runtime: AppleContainerRuntime | None = None,
        *,
        min_idle: int | None = None,
        max_total: int | None = None,
        max_per_owner: int | None = None,
    ) -> None:
        self.settings = settings
        self.runtime = runtime or AppleContainerRuntime(settings)
        self.min_idle = int(
            min_idle if min_idle is not None else getattr(settings, "apple_pool_min", 1)
        )
        self.max_total = int(
            max_total if max_total is not None else getattr(settings, "apple_pool_max", 4)
        )
        self.max_per_owner = int(
            max_per_owner
            if max_per_owner is not None
            else getattr(settings, "apple_pool_per_user", 2)
        )
        self._idle: list[PoolLease] = []
        self._active: dict[str, PoolLease] = {}
        self._by_key: dict[str, PoolLease] = {}
        self._lock = threading.RLock()

    def _new_lease(self, owner: str = "") -> PoolLease:
        key = f"pool:{uuid.uuid4().hex}"
        info = self.runtime.ensure(
            key,
            command=["sleep", "infinity"],
            create_args=POOL_CREATE_ARGS,
        )
        return PoolLease(owner=owner, info=info, key=key, acquired_at=time.time())

    def warm(self, count: int | None = None) -> int:
        target = max(self.min_idle, int(count or 0))
        with self._lock:
            while len(self._idle) < target and len(self._by_key) < self.max_total:
                lease = self._new_lease()
                self._idle.append(lease)
                self._by_key[lease.key] = lease
            return len(self._idle)

    def acquire(self, owner: str = "anonymous") -> PoolLease:
        owner = owner or "anonymous"
        with self._lock:
            active_for_owner = sum(
                1 for lease in self._active.values() if lease.owner == owner
            )
            if active_for_owner >= self.max_per_owner:
                raise PoolExhausted(
                    f"owner {owner!r} 已达到容器配额 {self.max_per_owner}"
                )
            lease = None
            if self._idle:
                lease = self._idle.pop(0)
            elif len(self._by_key) < self.max_total:
                lease = self._new_lease()
                self._by_key[lease.key] = lease
            if lease is None:
                raise PoolExhausted(f"容器池已满（{self.max_total}）")
            lease.owner = owner
            lease.acquired_at = time.time()
            self._active[lease.key] = lease
            return lease

    def release(self, lease: PoolLease, *, broken: bool = False) -> None:
        with self._lock:
            self._active.pop(lease.key, None)
            if broken:
                self._by_key.pop(lease.key, None)
                try:
                    self.runtime.delete(lease.key, delete_volume=True)
                except Exception:  # noqa: BLE001
                    pass
                return
            lease.broken = False
            self._idle.append(lease)

    def exec(
        self,
        lease: PoolLease,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = 30,
        workdir: str | None = None,
    ):
        if lease.broken or lease.key not in self._by_key:
            raise ContainerRuntimeError("lease 已失效")
        args = ["exec"]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        if workdir:
            args += ["-w", workdir]
        args += [lease.info.container_id or lease.info.name, *command]
        try:
            return self.runtime._run(args, timeout=timeout)
        except ContainerRuntimeError:
            lease.broken = True
            raise

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "idle": len(self._idle),
                "active": len(self._active),
                "total": len(self._by_key),
                "min_idle": self.min_idle,
                "max_total": self.max_total,
                "max_per_owner": self.max_per_owner,
                "owners": {
                    owner: sum(1 for lease in self._active.values() if lease.owner == owner)
                    for owner in {lease.owner for lease in self._active.values()}
                },
            }

    def close(self) -> None:
        with self._lock:
            for lease in list(self._by_key.values()):
                try:
                    self.runtime.delete(lease.key, delete_volume=True)
                except Exception:  # noqa: BLE001
                    pass
            self._by_key.clear()
            self._idle.clear()
            self._active.clear()
