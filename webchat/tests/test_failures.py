"""Tests for failure classification, retry policy, breaker and cancellation."""
from __future__ import annotations

import unittest

from webchat.runtime.cancel import ActiveRuns, CancellationToken
from webchat.runtime.failures import (
    CircuitBreaker,
    CircuitOpenError,
    FailureClass,
    RetryPolicy,
    RunCancelled,
    classify_exception,
)


class FailurePolicyTest(unittest.TestCase):
    def test_classifies_timeout_rate_limit_and_auth(self):
        self.assertEqual(classify_exception(TimeoutError("timed out")).kind, FailureClass.TIMEOUT)
        self.assertEqual(classify_exception(RuntimeError("rate limit exceeded")).kind, FailureClass.RATE_LIMIT)
        self.assertEqual(classify_exception(RuntimeError("invalid token")).kind, FailureClass.AUTH)
        self.assertEqual(classify_exception(RunCancelled("cancelled")).kind, FailureClass.CANCELLED)

    def test_retry_policy_retries_transient_only(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("temporary")
            return "ok"

        self.assertEqual(RetryPolicy(attempts=3, base_delay=0).run(flaky), "ok")
        self.assertEqual(calls["n"], 3)

        calls["n"] = 0

        def bad():
            calls["n"] += 1
            raise ValueError("invalid input")

        with self.assertRaises(ValueError):
            RetryPolicy(attempts=3, base_delay=0).run(bad)
        self.assertEqual(calls["n"], 1)

    def test_breaker_opens_and_half_opens(self):
        breaker = CircuitBreaker("x", failure_threshold=2, reset_timeout=1)
        breaker.failure(classify_exception(ConnectionError("x")))
        breaker.check()
        breaker.failure(classify_exception(ConnectionError("x")))
        with self.assertRaises(CircuitOpenError):
            breaker.check()

    def test_cancellation_token_and_registry(self):
        token = CancellationToken()
        token.check()
        token.cancel()
        with self.assertRaises(RunCancelled):
            token.check()

        runs = ActiveRuns()
        first = runs.begin("conv")
        self.assertTrue(runs.cancel("conv"))
        self.assertTrue(first.cancelled)
        second = runs.begin("conv")
        self.assertTrue(runs.is_active("conv"))
        runs.finish("conv", second)
        self.assertFalse(runs.is_active("conv"))
        self.assertFalse(runs.cancel("conv"))


if __name__ == "__main__":
    unittest.main()
