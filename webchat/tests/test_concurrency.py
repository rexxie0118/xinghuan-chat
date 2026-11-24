"""Tests for bounded concurrency / admission control."""
from __future__ import annotations

import asyncio
import unittest

from webchat.concurrency import (
    ConcurrencyGate,
    QueueFullError,
    QueueTimeoutError,
)


class ConcurrencyGateTest(unittest.TestCase):
    def test_queue_full_fails_fast(self):
        async def scenario():
            gate = ConcurrencyGate(
                max_concurrent=1, max_per_user=1, max_queue=0, queue_timeout=0.1
            )
            lease = await gate.acquire("u1")
            self.assertEqual(gate.metrics().active, 1)
            with self.assertRaises(QueueFullError):
                await gate.acquire("u2")
            lease.release()
            await asyncio.sleep(0)
            self.assertEqual(gate.metrics().active, 0)

        asyncio.run(scenario())

    def test_global_queue_timeout(self):
        async def scenario():
            gate = ConcurrencyGate(
                max_concurrent=1, max_per_user=1, max_queue=2, queue_timeout=0.03
            )
            lease = await gate.acquire("u1")
            with self.assertRaises(QueueTimeoutError):
                await gate.acquire("u2")
            lease.release()
            await asyncio.sleep(0)
            self.assertEqual(gate.metrics().waiting, 0)
            self.assertEqual(gate.metrics().active, 0)

        asyncio.run(scenario())

    def test_per_user_limit(self):
        async def scenario():
            gate = ConcurrencyGate(
                max_concurrent=2, max_per_user=1, max_queue=2, queue_timeout=0.03
            )
            first = await gate.acquire("same-user")
            with self.assertRaises(QueueTimeoutError):
                await gate.acquire("same-user")
            other = await gate.acquire("other-user")
            self.assertEqual(gate.metrics().active, 2)
            first.release()
            other.release()
            await asyncio.sleep(0)
            self.assertEqual(gate.metrics().active, 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
