"""Tests for queue and object-store adapters that do not need Redis/S3."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from webchat.db import _pg_sql
from webchat.jobs import InMemoryJobQueue, Job, RedisJobQueue, new_job
from webchat.object_store import build_artifact_store


class InfraTest(unittest.TestCase):
    def test_pg_placeholder_translation(self):
        self.assertEqual(
            _pg_sql("SELECT * FROM users WHERE id=? AND name=?"),
            "SELECT * FROM users WHERE id=%s AND name=%s",
        )

    def test_in_memory_job_queue(self):
        queue = InMemoryJobQueue()
        job = new_job("u1", "c1", kind="agent_run", payload={"content": "hi"})
        queue.enqueue(job)
        got = queue.dequeue(timeout=0.1)
        self.assertIsNotNone(got)
        self.assertEqual(got.id, job.id)
        self.assertEqual(got.status, "running")
        queue.complete(job, {"message_id": "m1"})
        self.assertEqual(queue.get(job.id).status, "completed")
        self.assertEqual(queue.get(job.id).result["message_id"], "m1")

    def test_redis_dequeue_timeout_is_idle_poll(self):
        class FakeRedis:
            def lrange(self, *_args, **_kwargs):
                return []

            def brpoplpush(self, *_args, **_kwargs):
                raise TimeoutError("idle")

        queue = object.__new__(RedisJobQueue)
        queue.redis = FakeRedis()
        queue.ready = "webchat:jobs:ready"
        queue.processing = "webchat:jobs:processing"
        queue._redis_errors = (TimeoutError,)
        self.assertIsNone(queue.dequeue(timeout=1))

    def test_redis_retry_and_dead_letter(self):
        class FakeRedis:
            def __init__(self):
                self.hashes = {}
                self.lists = {"ready": [], "processing": [], "dead": []}

            def hgetall(self, key):
                return dict(self.hashes.get(key, {}))

            def hset(self, key, mapping=None, **_kwargs):
                self.hashes.setdefault(key, {}).update(mapping or {})

            def lrem(self, name, _count, value):
                self.lists[name] = [item for item in self.lists[name] if item != value]

            def lpush(self, name, value):
                self.lists[name].insert(0, value)

        queue = object.__new__(RedisJobQueue)
        queue.redis = FakeRedis()
        queue.ready = "ready"
        queue.processing = "processing"
        queue.dead = "dead"
        queue.key = "job"
        queue.lease_seconds = 600
        queue._redis_errors = (TimeoutError,)
        job = Job("j1", "u1", "c1", "agent_run", {}, attempts=1, max_retries=3)
        queue.redis.hashes["job:j1"] = {"attempts": "1", "max_retries": "3"}
        queue.fail(job, "transient")
        self.assertEqual(job.status, "queued")
        self.assertIn("j1", queue.redis.lists["ready"])
        job.attempts = 4
        queue.fail(job, "permanent")
        self.assertEqual(job.status, "failed")
        self.assertIn("j1", queue.redis.lists["dead"])

    def test_redis_expired_lease_is_requeued(self):
        class FakeRedis:
            def __init__(self):
                self.lists = {"processing": ["j1"], "ready": [], "dead": []}
                self.hashes = {"job:j1": {"attempts": "1", "max_retries": "3", "lease_until": "1"}}

            def lrange(self, name, _start, _end):
                return list(self.lists[name])

            def hgetall(self, key):
                return dict(self.hashes.get(key, {}))

            def lrem(self, name, _count, value):
                self.lists[name] = [item for item in self.lists[name] if item != value]

            def lpush(self, name, value):
                self.lists[name].insert(0, value)

            def hset(self, key, mapping=None, **_kwargs):
                self.hashes.setdefault(key, {}).update(mapping or {})

        queue = object.__new__(RedisJobQueue)
        queue.redis = FakeRedis()
        queue.processing = "processing"
        queue.ready = "ready"
        queue.dead = "dead"
        queue.key = "job"
        queue._redis_errors = (TimeoutError,)
        self.assertEqual(queue.requeue_expired(), 1)
        self.assertIn("j1", queue.redis.lists["ready"])
        self.assertEqual(queue.redis.hashes["job:j1"]["status"], "queued")

    def test_local_artifact_store_factory(self):
        tmp = Path(tempfile.mkdtemp(prefix="object-test-"))
        try:
            settings = SimpleNamespace(data_dir=tmp, object_store="local")
            store = build_artifact_store(settings)
            self.assertTrue(store.root.is_dir())
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
