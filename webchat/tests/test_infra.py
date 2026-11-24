"""Tests for queue and object-store adapters that do not need Redis/S3."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from webchat.db import _pg_sql
from webchat.jobs import InMemoryJobQueue, new_job
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
