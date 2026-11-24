"""Bounded concurrency, admission control and backpressure for chat runs."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any


class QueueFullError(RuntimeError):
    pass


class QueueTimeoutError(RuntimeError):
    pass


@dataclass
class GateMetrics:
    active: int
    waiting: int
    max_concurrent: int
    max_per_user: int
    max_queue: int
    queue_timeout: float
    run_timeout: float

    def public(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "waiting": self.waiting,
            "max_concurrent": self.max_concurrent,
            "max_per_user": self.max_per_user,
            "max_queue": self.max_queue,
            "queue_timeout": self.queue_timeout,
            "run_timeout": self.run_timeout,
        }


class GateLease:
    def __init__(self, gate: "ConcurrencyGate", user_id: str, user_sem: asyncio.Semaphore) -> None:
        self._gate = gate
        self._user_id = user_id
        self._user_sem = user_sem
        self._loop = asyncio.get_running_loop()
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._loop.call_soon_threadsafe(
            self._gate._release, self._user_id, self._user_sem
        )


class ConcurrencyGate:
    """Global + per-user semaphores with a bounded waiting room.

    ``acquire`` is async and may wait up to ``queue_timeout``. If the waiting
    room is full, callers fail fast with ``QueueFullError`` so the HTTP layer can
    return 429 instead of growing memory.
    """

    def __init__(
        self,
        *,
        max_concurrent: int = 4,
        max_per_user: int = 1,
        max_queue: int = 32,
        queue_timeout: float = 30.0,
        run_timeout: float = 180.0,
    ) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_per_user = max(1, int(max_per_user))
        self.max_queue = max(0, int(max_queue))
        self.queue_timeout = max(0.1, float(queue_timeout))
        self.run_timeout = max(1.0, float(run_timeout))
        self._global = asyncio.Semaphore(self.max_concurrent)
        self._users: dict[str, asyncio.Semaphore] = {}
        self._waiting = 0
        self._active = 0
        self._lock = threading.Lock()

    def _user_sem(self, user_id: str) -> asyncio.Semaphore:
        sem = self._users.get(user_id)
        if sem is None:
            sem = asyncio.Semaphore(self.max_per_user)
            self._users[user_id] = sem
        return sem

    async def acquire(self, user_id: str) -> GateLease:
        with self._lock:
            # A free global permit may be taken immediately even with no waiting
            # room; only a request that would block is rejected.
            if self._global.locked() and self._waiting >= self.max_queue:
                raise QueueFullError(f"排队已满（{self.max_queue}）")
            self._waiting += 1
        global_acquired = False
        user_acquired = False
        user_sem = None
        try:
            try:
                await asyncio.wait_for(self._global.acquire(), self.queue_timeout)
                global_acquired = True
            except asyncio.TimeoutError as exc:
                raise QueueTimeoutError("等待全局执行槽位超时") from exc
            user_sem = self._user_sem(user_id)
            try:
                await asyncio.wait_for(user_sem.acquire(), self.queue_timeout)
                user_acquired = True
            except asyncio.TimeoutError as exc:
                raise QueueTimeoutError("等待用户执行槽位超时") from exc
            with self._lock:
                self._active += 1
            return GateLease(self, user_id, user_sem)
        except Exception:
            if user_acquired and user_sem is not None:
                user_sem.release()
            if global_acquired:
                self._global.release()
            raise
        finally:
            with self._lock:
                self._waiting = max(0, self._waiting - 1)

    def _release(self, user_id: str, user_sem: asyncio.Semaphore) -> None:
        user_sem.release()
        self._global.release()
        with self._lock:
            self._active = max(0, self._active - 1)

    def metrics(self) -> GateMetrics:
        with self._lock:
            active = self._active
            waiting = self._waiting
        return GateMetrics(
            active=active,
            waiting=waiting,
            max_concurrent=self.max_concurrent,
            max_per_user=self.max_per_user,
            max_queue=self.max_queue,
            queue_timeout=self.queue_timeout,
            run_timeout=self.run_timeout,
        )
