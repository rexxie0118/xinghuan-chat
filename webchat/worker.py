"""Standalone Worker for queued Agent jobs.

Run in a separate process:

    WEBCHAT_QUEUE_URL=redis://127.0.0.1:6379/0 python -m webchat.worker

Web only enqueues ``agent_run`` jobs; this process owns execution and writes the
assistant message back to the shared Postgres/SQLite database.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

from . import repository as repo
from .agent import AgentRunner
from .agent_defs import get_agent
from .artifacts import record_run
from .config import Settings
from .db import connect, init_db
from .jobs import RedisJobQueue
from .knowledge import KnowledgeRegistry
from .runtime import AgentKernel
from .sandbox import build_sandbox_runner
from .tools import build_tool_registry
from .user_memory import UserMemoryService

LOG = logging.getLogger("webchat.worker")
_stop = False


def _handle_signal(_signum, _frame):
    global _stop
    _stop = True


class Worker:
    def __init__(self, settings: Settings, queue: RedisJobQueue) -> None:
        self.settings = settings
        self.queue = queue
        self.registry = KnowledgeRegistry(settings)
        sandbox = build_sandbox_runner(settings)
        tools = build_tool_registry(settings)
        self.memory = UserMemoryService(settings.db_path)
        runner = AgentRunner(settings, sandbox, tools, memory_store=self.memory)
        self.kernel = AgentKernel(settings, runner)

    def _kb_for(self, user: dict, conversation: dict):
        allowed = self.registry.for_user(user)
        for service in allowed:
            if service.info.id == conversation["kb_id"]:
                return service
        return None

    def process(self, job) -> None:
        db = connect(self.settings.db_path)
        try:
            user_row = repo.get_user(db, job.user_id)
            if user_row is None:
                self.queue.fail(job, "用户不存在")
                return
            user = repo.user_public(user_row)
            conversation = repo.get_conversation(
                db, conversation_id=job.conversation_id, user_id=job.user_id
            )
            if conversation is None:
                self.queue.fail(job, "会话不存在")
                return
            kb = self._kb_for(user, conversation)
            if kb is None:
                self.queue.fail(job, "没有知识库访问权限")
                return
            imported_agent = None
            agent_id = str(job.payload.get("agent_id") or "")
            if agent_id:
                imported_agent = get_agent(db, agent_id, user)
                if imported_agent is None:
                    self.queue.fail(job, "Agent 不存在")
                    return
            history = repo.conversation_context(
                db, conversation_id=job.conversation_id, user_id=job.user_id, limit=8
            )
            question = str(job.payload.get("content") or "").strip()
            if not question:
                self.queue.fail(job, "消息为空")
                return
            result = self.kernel.run(
                question=question,
                mode=str(job.payload.get("mode") or "agent"),
                user=user,
                conversation=conversation,
                history=history,
                kb=kb,
                imported_agent=imported_agent,
            )
            meta = result.message_meta()
            execution = (meta or {}).get("execution") or {}
            if execution.get("run_id"):
                record_run(
                    db,
                    run_id=execution["run_id"],
                    user_id=job.user_id,
                    agent_id=str(meta.get("agent_id") or ""),
                    agent_name=str(meta.get("agent_name") or ""),
                    execution=execution,
                )
            message = repo.add_message(
                db,
                conversation_id=job.conversation_id,
                user_id=job.user_id,
                role="assistant",
                content=result.answer,
                citations=result.citations,
                meta=meta,
            )
            self.queue.complete(
                job,
                {
                    "message_id": message["id"],
                    "content": result.answer,
                    "meta": meta,
                },
            )
        except Exception as exc:  # noqa: BLE001
            LOG.exception("job failed: %s", job.id)
            self.queue.fail(job, f"{type(exc).__name__}: {exc}")
        finally:
            db.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    init_db(settings.db_path)
    url = (os.getenv("WEBCHAT_QUEUE_URL") or getattr(settings, "queue_url", "") or "").strip()
    if not url:
        print("WEBCHAT_QUEUE_URL is required", file=sys.stderr)
        return 2
    queue = RedisJobQueue(url, name=os.getenv("WEBCHAT_QUEUE_NAME", "webchat:jobs"))
    worker = Worker(settings, queue)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    LOG.info("worker started")
    while not _stop:
        job = queue.dequeue(timeout=5)
        if job is None:
            continue
        worker.process(job)
    queue.close()
    LOG.info("worker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
