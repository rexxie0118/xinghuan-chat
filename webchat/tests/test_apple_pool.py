"""Tests for the warm Apple Container pool and owner quota."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from webchat.sandbox.apple_pool import AppleContainerPool, PoolExhausted
from webchat.sandbox.apple_runtime import RuntimeInfo


class FakeRuntime:
    def __init__(self):
        self.created = 0
        self.deleted = []

    def ensure(self, key, **_kwargs):
        self.created += 1
        return RuntimeInfo(key, f"xh-{key}", f"vol-{key}", "python:3.12-slim", "running", f"xh-{key}")

    def delete(self, key, **_kwargs):
        self.deleted.append(key)
        return True

    def _run(self, args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")


class ApplePoolTest(unittest.TestCase):
    def setUp(self):
        self.runtime = FakeRuntime()
        self.pool = AppleContainerPool(
            SimpleNamespace(),
            self.runtime,
            min_idle=1,
            max_total=2,
            max_per_owner=1,
        )

    def test_warm_and_owner_quota(self):
        self.assertEqual(self.pool.warm(1), 1)
        first = self.pool.acquire("alice")
        self.assertEqual(self.pool.status()["active"], 1)
        with self.assertRaises(PoolExhausted):
            self.pool.acquire("alice")
        second = self.pool.acquire("bob")
        self.assertEqual(self.pool.status()["active"], 2)
        with self.assertRaises(PoolExhausted):
            self.pool.acquire("carol")
        self.pool.release(first)
        self.assertEqual(self.pool.status()["idle"], 1)
        self.pool.release(second)
        self.assertEqual(self.pool.status()["active"], 0)

    def test_close_cleans_containers(self):
        self.pool.warm(1)
        self.pool.acquire("alice")
        self.pool.close()
        self.assertTrue(self.runtime.deleted)


if __name__ == "__main__":
    unittest.main()
