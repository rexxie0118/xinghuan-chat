"""Durable-ish job queue for splitting Web and Worker.

Redis is optional: local tests use InMemoryJobQueue; production uses Redis lists
plus a job hash. The queue deliberately does not import FastAPI or the kernel.
"""

from __future__ import annotations

import json
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

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class JobQueue:
    def enqueue(self, job: Job) -> None:
        raise NotImplementedError

    def dequeue(self, timeout: int = 5) -> Job | None:
        raise NotImplementedError

    def complete(self, job: Job, result: dict[str, Any]) -> None:
        raise NotImplementedError

    def fail(self, job: Job, error: str) -> None:
        raise NotImplementedError

    def get(self, job_id: str) -> Job | None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class InMemoryJobQueue(JobQueue):
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._ready: list[str] = []
        self._cv = threading.Condition()

    def enqueue(self, job: Job) -> None:
        with self._cv:
            self._jobs[job.id] = job
            self._ready.append(job.id)
            self._cv.notify()

    def dequeue(self, timeout: int = 5) -> Job | None:
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._ready:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            job = self._jobs[self._ready.pop(0)]
            job.status = "running"
            return job

    def complete(self, job: Job, result: dict[str, Any]) -> None:
        job.status = "completed"
        job.result = result
        job.updated_at = time.time()

    def fail(self, job: Job, error: str) -> None:
        job.status = "failed"
        job.error = error[:1000]
        job.updated_at = time.time()

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)


class RedisJobQueue(JobQueue):
    def __init__(self, url: str, *, name: str = "webchat:jobs") -> None:
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
        self.key = f"{name}:job"

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
            },
        )
        pipe.expire(self._key(job.id), 86400 * 7)
        pipe.lpush(self.ready, job.id)
        pipe.execute()

    def dequeue(self, timeout: int = 5) -> Job | None:
        try:
            item = self.redis.brpop(self.ready, timeout=timeout)
        except self._redis_errors:
            # Blocking pop can race the client-side socket timeout. Treat it as
            # an idle poll; the next loop iteration reconnects if needed.
            return None
        if not item:
            return None
        job_id = item[1]
        raw = self.redis.hgetall(self._key(job_id))
        if not raw:
            return None
        self.redis.hset(self._key(job_id), "status", "running")
        return Job(
            id=job_id,
            user_id=raw.get("user_id", ""),
            conversation_id=raw.get("conversation_id", ""),
            kind=raw.get("kind", "agent_run"),
            payload=json.loads(raw.get("payload") or "{}"),
            status="running",
            created_at=float(raw.get("created_at") or time.time()),
        )

    def _finish(self, job: Job, status: str, *, result: dict[str, Any] | None = None, error: str = "") -> None:
        job.status = status
        job.result = result or {}
        job.error = error
        job.updated_at = time.time()
        self.redis.hset(
            self._key(job.id),
            mapping={
                "status": status,
                "result": json.dumps(result or {}, ensure_ascii=False),
                "error": error[:1000],
                "updated_at": str(job.updated_at),
            },
        )

    def complete(self, job: Job, result: dict[str, Any]) -> None:
        self._finish(job, "completed", result=result)

    def fail(self, job: Job, error: str) -> None:
        self._finish(job, "failed", error=error)

    def get(self, job_id: str) -> Job | None:
        raw = self.redis.hgetall(self._key(job_id))
        if not raw:
            return None
        return Job(
            id=job_id,
            user_id=raw.get("user_id", ""),
            conversation_id=raw.get("conversation_id", ""),
            kind=raw.get("kind", "agent_run"),
            payload=json.loads(raw.get("payload") or "{}"),
            status=raw.get("status", "queued"),
            result=json.loads(raw.get("result") or "{}"),
            error=raw.get("error", ""),
            created_at=float(raw.get("created_at") or time.time()),
            updated_at=float(raw.get("updated_at") or time.time()),
        )

    def close(self) -> None:
        self.redis.close()


def new_job(user_id: str, conversation_id: str, *, kind: str, payload: dict[str, Any]) -> Job:
    return Job(
        id=f"job_{uuid.uuid4().hex}",
        user_id=user_id,
        conversation_id=conversation_id,
        kind=kind,
        payload=payload,
    )
