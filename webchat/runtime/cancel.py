"""Cancellation tokens for in-flight chat runs."""

from __future__ import annotations

import threading
from typing import Callable

from .failures import RunCancelled


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason = ""

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "") -> None:
        if reason:
            self._reason = reason
        self._event.set()

    def check(self) -> None:
        if self._event.is_set():
            raise RunCancelled(self._reason or "本次生成已取消")

    def on_cancel(self, callback: Callable[[], None]) -> None:
        """Best-effort callback support; the token itself stays thread-safe."""
        if self._event.is_set():
            callback()
            return
        # Polling callbacks are intentionally not registered globally; callers
        # that need to kill a subprocess should poll ``cancelled``.
        _ = callback


class ActiveRuns:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, CancellationToken] = {}

    def begin(self, key: str) -> CancellationToken:
        with self._lock:
            previous = self._runs.get(key)
            if previous is not None:
                previous.cancel()
            token = CancellationToken()
            self._runs[key] = token
            return token

    def cancel(self, key: str, reason: str = "") -> bool:
        with self._lock:
            token = self._runs.get(key)
            if token is None:
                return False
            token.cancel(reason)
            return True

    def finish(self, key: str, token: CancellationToken) -> None:
        with self._lock:
            if self._runs.get(key) is token:
                self._runs.pop(key, None)

    def is_active(self, key: str) -> bool:
        with self._lock:
            token = self._runs.get(key)
            return token is not None and not token.cancelled
