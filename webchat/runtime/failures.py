"""Failure classification, retry policy and a small circuit breaker.

These are runtime primitives, not a workflow engine. They give the kernel a
consistent vocabulary for transient/rate-limit/auth/cancel errors and let
callers retry only the classes that are safe to retry.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar

import httpx

LOG = logging.getLogger("webchat.runtime.failures")
T = TypeVar("T")


class FailureClass(str, enum.Enum):
    TRANSIENT = "transient"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    AUTH = "auth"
    PERMISSION = "permission"
    INPUT = "input"
    UPSTREAM = "upstream"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class Failure:
    kind: FailureClass
    message: str
    retryable: bool = False
    status_code: int | None = None
    retry_after: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "message": self.message[:600],
            "retryable": self.retryable,
            "status_code": self.status_code,
            "retry_after": self.retry_after,
        }


def classify_exception(exc: BaseException) -> Failure:
    if isinstance(exc, RunCancelled):
        return Failure(FailureClass.CANCELLED, str(exc), retryable=False)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return Failure(FailureClass.TIMEOUT, str(exc), retryable=True)
    if isinstance(exc, httpx.TimeoutException):
        return Failure(FailureClass.TIMEOUT, str(exc), retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429:
            retry_after = _retry_after(exc.response.headers.get("retry-after"))
            return Failure(
                FailureClass.RATE_LIMIT,
                f"HTTP 429: {exc}",
                retryable=True,
                status_code=429,
                retry_after=retry_after,
            )
        if status in {401, 407}:
            return Failure(FailureClass.AUTH, f"HTTP {status}", status_code=status)
        if status == 403:
            return Failure(FailureClass.PERMISSION, "HTTP 403", status_code=403)
        if 400 <= status < 500:
            return Failure(FailureClass.INPUT, f"HTTP {status}", status_code=status)
        return Failure(
            FailureClass.UPSTREAM,
            f"HTTP {status}",
            retryable=status >= 500,
            status_code=status,
        )
    if isinstance(exc, (ConnectionError, OSError)):
        return Failure(FailureClass.TRANSIENT, str(exc), retryable=True)
    text = str(exc).lower()
    if "rate limit" in text or "too many requests" in text:
        return Failure(FailureClass.RATE_LIMIT, str(exc), retryable=True)
    if "timeout" in text or "timed out" in text:
        return Failure(FailureClass.TIMEOUT, str(exc), retryable=True)
    if "unauthorized" in text or "invalid token" in text or "api key" in text:
        return Failure(FailureClass.AUTH, str(exc), retryable=False)
    if "permission" in text or "forbidden" in text:
        return Failure(FailureClass.PERMISSION, str(exc), retryable=False)
    if "invalid" in text or "validation" in text or "bad request" in text:
        return Failure(FailureClass.INPUT, str(exc), retryable=False)
    if "connection" in text or "temporarily" in text or "unavailable" in text:
        return Failure(FailureClass.TRANSIENT, str(exc), retryable=True)
    return Failure(FailureClass.UNKNOWN, str(exc), retryable=False)


def classify_execution(execution: dict[str, Any]) -> Failure | None:
    if execution.get("timed_out"):
        return Failure(FailureClass.TIMEOUT, "脚本执行超时", retryable=False)
    if execution.get("ran") and execution.get("exit_code") not in (0, None):
        return Failure(
            FailureClass.UPSTREAM,
            f"脚本 exit={execution.get('exit_code')}",
            retryable=False,
        )
    return None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class RunCancelled(RuntimeError):
    """Raised when the caller cancels an in-flight run."""


class RetryPolicy:
    def __init__(
        self,
        *,
        attempts: int = 3,
        base_delay: float = 0.25,
        max_delay: float = 4.0,
        retry_on: Iterable[FailureClass] = (
            FailureClass.TRANSIENT,
            FailureClass.RATE_LIMIT,
            FailureClass.TIMEOUT,
            FailureClass.UPSTREAM,
        ),
    ) -> None:
        self.attempts = max(1, int(attempts))
        self.base_delay = max(0.0, float(base_delay))
        self.max_delay = max(0.0, float(max_delay))
        self.retry_on = set(retry_on)

    def run(self, fn: Callable[[], T], *, breaker: "CircuitBreaker | None" = None) -> T:
        last: BaseException | None = None
        for attempt in range(self.attempts):
            if breaker is not None:
                breaker.check()
            try:
                result = fn()
                if breaker is not None:
                    breaker.success()
                return result
            except BaseException as exc:  # noqa: BLE001
                last = exc
                failure = classify_exception(exc)
                if breaker is not None:
                    breaker.failure(failure)
                if attempt >= self.attempts - 1 or failure.kind not in self.retry_on:
                    raise
                delay = failure.retry_after
                if delay is None:
                    delay = min(self.max_delay, self.base_delay * (2**attempt))
                LOG.warning("retrying after %s: %s", failure.kind.value, exc)
                time.sleep(max(0.0, delay))
        assert last is not None
        raise last


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    """A tiny per-dependency breaker with half-open probing."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        reset_timeout: float = 30.0,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.reset_timeout = max(1.0, float(reset_timeout))
        self.failures = 0
        self.opened_at = 0.0
        self.state = "closed"

    def check(self) -> None:
        if self.state != "open":
            return
        if time.monotonic() - self.opened_at >= self.reset_timeout:
            self.state = "half-open"
            return
        raise CircuitOpenError(f"{self.name} circuit is open")

    def success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.opened_at = 0.0

    def failure(self, failure: Failure | None = None) -> None:
        if failure is not None and not failure.retryable:
            return
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.state = "open"
            self.opened_at = time.monotonic()
