"""Durable-ish job queue for splitting Web and Worker.

Redis is optional: local tests use InMemoryJobQueue; production uses a ready
list, a processing list, job hashes, leases, retry and a dead-letter list. The
queue deliberately does not import FastAPI or the kernel.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    id: str
    user_id: str
    conversation_id: str
    kind: str
    payload: dict[str, Any]
    status: str = "queued"
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    attempts: int = 0
    max_retries: int = 3
    lease_until: float = 0.0
    worker_id: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "max_retries": self.max_retries,
        }


class JobQueue:
    def enqueue(self, job: Job) -> None:
        raise NotImplementedError

    def dequeue(self, timeout: int = 5, *, worker_id: str = "") -> Job | None:
        raise NotImplementedError

    def complete(self, job: Job, result: dict[str, Any]) -> None:
        raise NotImplementedError

    def fail(self, job: Job, error: str) -> None:
        raise NotImplementedError

    def get(self, job_id: str) -> Job | None:
        raise NotImplementedError

    def metrics(self) -> dict[str, int]:
        return {"ready": 0, "processing": 0, "dead": 0}

    def close(self) -> None:
        pass


class InMemoryJobQueue(JobQueue):
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._ready: list[str] = []
        self._processing: set[str] = set()
        self._dead: set[str] = set()
        self._cv = threading.Condition()

    def enqueue(self, job: Job) -> None:
        with self._cv:
            self._jobs[job.id] = job
            self._ready.append(job.id)
            self._cv.notify()

    def dequeue(self, timeout: int = 5, *, worker_id: str = "") -> Job | None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._ready:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            job = self._jobs[self._ready.pop(0)]
            job.status = "running"
            job.attempts += 1
            job.worker_id = worker_id
            self._processing.add(job.id)
            return job

    def complete(self, job: Job, result: dict[str, Any]) -> None:
        job.status = "completed"
        job.result = result
        job.updated_at = time.time()
        self._processing.discard(job.id)

    def fail(self, job: Job, error: str) -> None:
        job.status = "failed"
        job.error = error[:1000]
        job.updated_at = time.time()
        self._processing.discard(job.id)
        self._dead.add(job.id)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def metrics(self) -> dict[str, int]:
        return {
            "ready": len(self._ready),
            "processing": len(self._processing),
            "dead": len(self._dead),
        }


class RedisJobQueue(JobQueue):
    def __init__(
        self,
        url: str,
        *,
        name: str = "webchat:jobs",
        lease_seconds: int = 600,
    ) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - optional infra
            raise RuntimeError("Redis 队列需要安装 redis") from exc
        self.redis = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=15,
        )
        self._redis_errors = (redis.exceptions.TimeoutError, redis.exceptions.ConnectionError)
        self.name = name
        self.ready = f"{name}:ready"
        self.processing = f"{name}:processing"
        self.dead = f"{name}:dead"
        self.key = f"{name}:job"
        self.lease_seconds = max(30, int(lease_seconds))

    def _key(self, job_id: str) -> str:
        return f"{self.key}:{job_id}"

    def enqueue(self, job: Job) -> None:
        pipe = self.redis.pipeline()
        pipe.hset(
            self._key(job.id),
            mapping={
                "payload": json.dumps(job.payload, ensure_ascii=False),
                "user_id": job.user_id,
                "conversation_id": job.conversation_id,
                "kind": job.kind,
                "status": job.status,
                "created_at": str(job.created_at),
                "attempts": str(job.attempts),
                "max_retries": str(job.max_retries),
                "lease_until": "0",
            },
        )
        pipe.expire(self._key(job.id), 86400 * 7)
        pipe.lpush(self.ready, job.id)
        pipe.execute()

    def _read_job(self, job_id: str, *, status: str = "", **extra: Any) -> Job | None:
        raw = self.redis.hgetall(self._key(job_id))
        if not raw:
            return None
        return Job(
            id=job_id,
            user_id=raw.get("user_id", ""),
            conversation_id=raw.get("conversation_id", ""),
            kind=raw.get("kind", "agent_run"),
            payload=json.loads(raw.get("payload") or "{}"),
            status=status or raw.get("status", "queued"),
            result=json.loads(raw.get("result") or "{}"),
            error=raw.get("error", ""),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
            attempts=int(raw.get("attempts") or 0),
            max_retries=int(raw.get("max_retries") or 3),
            lease_until=float(raw.get("lease_until") or 0),
            worker_id=raw.get("worker_id", ""),
            **extra,
        )

    def dequeue(self, timeout: int = 5, *, worker_id: str = "") -> Job | None:
        self.requeue_expired()
        try:
            if hasattr(self.redis, "blmove"):
                job_id = self.redis.blmove(self.ready, self.processing, timeout, "RIGHT", "LEFT")
            else:  # pragma: no cover - old redis-py
                job_id = self.redis.brpoplpush(self.ready, self.processing, timeout)
        except self._redis_errors:
            # Blocking move can race the client-side socket timeout. Treat it as
            # an idle poll; the next loop iteration reconnects if needed.
            return None
        if not job_id:
            return None
        raw = self.redis.hgetall(self._key(job_id))
        if not raw:
            self.redis.lrem(self.processing, 1, job_id)
            return None
        attempts = int(raw.get("attempts") or 0) + 1
        now = time.time()
        self.redis.hset(
            self._key(job_id),
            mapping={
                "status": "running",
                "attempts": str(attempts),
                "lease_until": str(now + self.lease_seconds),
                "worker_id": worker_id,
                "updated_at": str(now),
            },
        )
        return Job(
            id=job_id,
            user_id=raw.get("user_id", ""),
            conversation_id=raw.get("conversation_id", ""),
            kind=raw.get("kind", "agent_run"),
            payload=json.loads(raw.get("payload") or "{}"),
            status="running",
            created_at=float(raw.get("created_at") or now),
            updated_at=now,
            attempts=attempts,
            max_retries=int(raw.get("max_retries") or 3),
            lease_until=now + self.lease_seconds,
            worker_id=worker_id,
        )

    def complete(self, job: Job, result: dict[str, Any]) -> None:
        now = time.time()
        job.status = "completed"
        job.result = result
        job.updated_at = now
        self.redis.hset(
            self._key(job.id),
            mapping={
                "status": "completed",
                "result": json.dumps(result or {}, ensure_ascii=False),
                "error": "",
                "lease_until": "0",
                "updated_at": str(now),
            },
        )
        self.redis.lrem(self.processing, 1, job.id)

    def fail(self, job: Job, error: str) -> None:
        raw = self.redis.hgetall(self._key(job.id))
        attempts = int(job.attempts or raw.get("attempts") or 1)
        max_retries = int(job.max_retries or raw.get("max_retries") or 3)
        now = time.time()
        self.redis.lrem(self.processing, 1, job.id)
        if attempts <= max_retries:
            status = "queued"
            self.redis.lpush(self.ready, job.id)
        else:
            status = "failed"
            self.redis.lpush(self.dead, job.id)
        job.status = status
        job.error = error[:1000]
        job.updated_at = now
        self.redis.hset(
            self._key(job.id),
            mapping={
                "status": status,
                "error": error[:1000],
                "attempts": str(attempts),
                "max_retries": str(max_retries),
                "lease_until": "0",
                "updated_at": str(now),
            },
        )

    def requeue_expired(self) -> int:
        """Recover jobs whose worker disappeared after the lease expired."""
        now = time.time()
        recovered = 0
        try:
            ids = self.redis.lrange(self.processing, 0, -1)
            for job_id in ids:
                raw = self.redis.hgetall(self._key(job_id))
                if not raw:
                    self.redis.lrem(self.processing, 1, job_id)
                    continue
                lease_until = float(raw.get("lease_until") or 0)
                if lease_until and lease_until > now:
                    continue
                attempts = int(raw.get("attempts") or 0)
                max_retries = int(raw.get("max_retries") or 3)
                self.redis.lrem(self.processing, 1, job_id)
                if attempts >= max_retries:
                    self.redis.lpush(self.dead, job_id)
                    self.redis.hset(
                        self._key(job_id),
                        mapping={
                            "status": "failed",
                            "error": "lease expired; retries exhausted",
                            "lease_until": "0",
                            "updated_at": str(now),
                        },
                    )
                else:
                    self.redis.lpush(self.ready, job_id)
                    self.redis.hset(
                        self._key(job_id),
                        mapping={
                            "status": "queued",
                            "error": "lease expired; requeued",
                            "lease_until": "0",
                            "updated_at": str(now),
                        },
                    )
                recovered += 1
        except self._redis_errors:
            return recovered
        return recovered

    def get(self, job_id: str) -> Job | None:
        return self._read_job(job_id)

    def metrics(self) -> dict[str, int]:
        try:
            return {
                "ready": int(self.redis.llen(self.ready)),
                "processing": int(self.redis.llen(self.processing)),
                "dead": int(self.redis.llen(self.dead)),
            }
        except self._redis_errors:
            return {"ready": 0, "processing": 0, "dead": 0}

    def close(self) -> None:
        self.redis.close()


def new_job(user_id: str, conversation_id: str, *, kind: str, payload: dict[str, Any]) -> Job:
    return Job(
        id=f"job_{uuid.uuid4().hex}",
        user_id=user_id,
        conversation_id=conversation_id,
        kind=kind,
        payload=payload,
        max_retries=max(0, int(os.getenv("WEBCHAT_JOB_MAX_RETRIES", "3"))),
    )
