"""FastAPI application for 星环 Chat."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote, urlparse

import httpx

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from . import repository as repo
from .agent import AgentRunner
from .artifacts import owns_run, record_run
from .object_store import build_artifact_store
from .agent_defs import (
    agent_detail,
    build_agent,
    delete_agent,
    get_agent,
    list_agents,
    save_agent,
    seed_department_agents,
)
from .github_importer import GitHubImportError, import_from_github
from .jobs import RedisJobQueue, new_job
from .concurrency import ConcurrencyGate, QueueFullError, QueueTimeoutError
from .connectors import ConnectorError, ConnectorService, known_for, parse_spec, propose_with_llm
from .config import Settings
from .db import connect, init_db, is_integrity_error
from .memory_semantic import LLMSemanticJudge
from .observability import AuditLog, new_request_id, request_id_var, user_id_var
from .knowledge import KnowledgeRegistry, KnowledgeService
from kb.ingest import SUPPORTED_SUFFIXES
from .sandbox import build_sandbox_runner
from .sandbox.apple_runtime import AppleContainerRuntime, ContainerRuntimeError
from .sandbox.apple_pool import AppleContainerPool
from .sandbox.apple_network import network_policy
from .container_workspaces import ContainerWorkspaceService, RuntimeReaper
from .tools import ToolError, build_tool_registry
from .websearch import register_web_tools
from .user_memory import MemoryConflict, UserMemoryService
from .runtime.cancel import ActiveRuns
from .runtime.failures import RunCancelled, classify_exception
from .runtime import AgentKernel
from .runtime.plugins import load_plugin_host
from .security import (
    COOKIE_NAME,
    hash_password,
    new_session_token,
    token_digest,
    verify_password,
)
from .util import expires_at

LOG = logging.getLogger("webchat")

APP_NAME = "星环 Chat"
DEMO_ACCOUNTS = [
    {"username": "demo", "password": "demo1234", "display_name": "演示用户", "department": "产品部"},
    {"username": "alice", "password": "alice1234", "display_name": "Alice", "department": "研发部"},
    {"username": "bob", "password": "bob1234", "display_name": "Bob", "department": "人事部"},
    {
        "username": "admin",
        "password": "admin1234",
        "display_name": "管理员",
        "department": "产品部",
        "role": "admin",
    },
]

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{3,24}$")


# ------------------------------------------------------------------ models


class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    password: str = Field(min_length=6, max_length=128)
    display_name: str | None = Field(default=None, max_length=30)
    department: str | None = Field(default=None, max_length=30)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ConversationIn(BaseModel):
    mode: Literal["rag", "agent"] = "agent"
    kb: str | None = Field(default=None, max_length=40)


class ConversationPatch(BaseModel):
    title: str = Field(min_length=1, max_length=40)


class AgentFileIn(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    content: str = Field(default="")


class AgentIn(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=200)
    kind: Literal["agent", "skill"] = "agent"
    entrypoint: str | None = Field(default=None, max_length=200)
    markdown: str = Field(min_length=1)
    files: list[AgentFileIn] = Field(default_factory=list)


class ToolCallIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class AgentTestIn(BaseModel):
    question: str = Field(default="测试执行：请输出 OK", min_length=1, max_length=2000)


class MemoryIn(BaseModel):
    kind: Literal["fact", "preference", "task"] = "fact"
    content: str = Field(min_length=1, max_length=300)
    subject: str = Field(default="", max_length=80)
    predicate: str = Field(default="", max_length=80)
    object_text: str = Field(default="", max_length=300)
    importance: float = Field(default=0.7, ge=0, le=1)
    confidence: float = Field(default=0.8, ge=0, le=1)
    scope: Literal["personal", "department", "org"] = "personal"
    scope_id: str = Field(default="", max_length=80)
    agent_id: str = Field(default="", max_length=80)


class ConnectorIn(BaseModel):
    spec: dict[str, Any]


class ConnectorProposeIn(BaseModel):
    query: str = Field(min_length=1, max_length=800)


class ConnectorTestIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class AdminUserPatch(BaseModel):
    role: Literal["admin", "manager", "member"] | None = None
    department: str | None = Field(default=None, max_length=40)


class JobIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    mode: Literal["rag", "agent"] = "agent"
    agent_id: str | None = Field(default=None, max_length=80)


class RuntimeRestoreIn(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=80)


class RuntimeEnsureIn(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=80)


class AgentConfigIn(BaseModel):
    entrypoint: str | None = Field(default=None, max_length=200)
    runtime: Literal["python", "bash", "node"] | None = None
    timeout: float | None = Field(default=None, ge=1, le=15)


class GitHubImportIn(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    ref: str | None = Field(default=None, max_length=120)
    path: str | None = Field(default=None, max_length=300)
    entrypoint: str | None = Field(default=None, max_length=200)
    kind: Literal["agent", "skill"] | None = None
    name: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=200)


class MessageIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    mode: Literal["rag", "agent"] = "agent"
    agent_id: str | None = Field(default=None, max_length=40)
    live: bool = False


# -------------------------------------------------------------------- utils


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunks(text: str, size: int = 14) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max(1, settings.session_days) * 86400,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookie,
        path="/",
    )


def _new_session(conn: sqlite3.Connection, user_id: str, settings: Settings) -> str:
    token = new_session_token()
    repo.save_session(
        conn,
        user_id=user_id,
        token_hash=token_digest(token),
        expires=expires_at(settings.session_days),
    )
    return token


def _seed_demo_users(conn: sqlite3.Connection) -> None:
    for account in DEMO_ACCOUNTS:
        row = repo.find_user_by_username(conn, account["username"])
        if row is None:
            repo.create_user(
                conn,
                username=account["username"],
                display_name=account["display_name"],
                password_hash=hash_password(account["password"]),
                department=account["department"],
                role=str(account.get("role") or "member"),
            )
            LOG.info("Seeded demo user %s (%s)", account["username"], account["department"])
        else:
            if row["department"] != account["department"]:
                # Upgrade databases seeded before department support.
                repo.set_user_department(conn, row["id"], account["department"])
            if account.get("role") == "admin" and (
                "role" not in row.keys() or row["role"] != "admin"
            ):
                repo.update_user_role_department(
                    conn, row["id"], role="admin", department=account["department"]
                )


def _clean_username(value: str) -> str:
    username = (value or "").strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=400,
            detail="用户名需为 3-24 位，只允许中英文、数字、下划线、点和短横线",
        )
    return username


# --------------------------------------------------------------------- app


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.ensure_dirs()
    init_db(settings.db_path)
    seed_conn = connect(settings.db_path)
    try:
        seed_department_agents(seed_conn)
    finally:
        seed_conn.close()

    registry = KnowledgeRegistry(settings)
    sandbox = build_sandbox_runner(settings)
    container_runtime = AppleContainerRuntime(settings)
    container_pool = None
    if settings.sandbox.startswith("apple") and settings.apple_pool_enabled:
        container_pool = AppleContainerPool(settings, container_runtime)
    workspace_service = ContainerWorkspaceService(settings, container_runtime)
    active_runs = ActiveRuns()
    gate = ConcurrencyGate(
        max_concurrent=settings.max_concurrent_runs,
        max_per_user=settings.max_user_runs,
        max_queue=settings.max_queue,
        queue_timeout=settings.queue_timeout,
        run_timeout=settings.run_timeout,
    )
    audit = AuditLog(settings.db_path)
    tools = build_tool_registry(settings, audit=audit)
    register_web_tools(tools, settings)
    connector_service = ConnectorService(settings, tools)
    try:
        connector_service.load_approved()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("connector load failed: %s", exc)
    semantic_judge = None
    if settings.memory_semantic in {"llm", "on", "true", "1"}:
        judge_service = registry.get(registry.infos[0].id)
        if judge_service is not None:
            semantic_judge = LLMSemanticJudge(judge_service.provider.chat)
            LOG.info("semantic memory merge enabled (LLM judge)")
    memory_store = UserMemoryService(settings.db_path, semantic_judge=semantic_judge)
    runner = AgentRunner(settings, sandbox, tools, memory_store=memory_store)
    kernel = AgentKernel(settings, runner)
    plugin_host = load_plugin_host(
        settings, kernel, tools, services={"tools": tools}
    )
    plugin_host.load_builtins()
    runner.attach_plugin_host(plugin_host)
    job_queue = None
    if settings.queue_url:
        job_queue = RedisJobQueue(settings.queue_url, name=settings.queue_name)

    conn = connect(settings.db_path)
    try:
        if settings.seed_demo_users:
            _seed_demo_users(conn)
        if settings.bootstrap_admin:
            admin = repo.find_user_by_username(conn, settings.bootstrap_admin)
            if admin is not None:
                repo.update_user_role_department(
                    conn,
                    admin["id"],
                    role="admin",
                    department=admin["department"],
                )
        repo.purge_expired_sessions(conn)
    finally:
        conn.close()

    async def memory_consolidation_loop(stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(60, settings.memory_consolidate_interval))
            except asyncio.TimeoutError:
                pass
            if stop.is_set():
                break
            try:
                result = await asyncio.to_thread(
                    memory_store.consolidate_all,
                    expire_days=settings.memory_consolidate_expire_days,
                )
                audit.record(
                    event_type="memory_consolidate_job",
                    component="memory",
                    action="scheduled",
                    status="ok",
                    metadata=result,
                )
                LOG.info("memory consolidation job: %s", result)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("memory consolidation job failed: %s", exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        LOG.info("星环 Chat ready at %s", settings.db_path)
        reaper_task = None
        reaper_stop = None
        memory_stop = asyncio.Event()
        memory_task = asyncio.create_task(memory_consolidation_loop(memory_stop))
        if container_pool is not None:
            try:
                container_pool.warm(settings.apple_pool_min)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Apple pool warm failed: %s", exc)
        if settings.sandbox.startswith("apple") and settings.runtime_idle_enabled:
            reaper_stop = asyncio.Event()
            reaper = RuntimeReaper(
                workspace_service,
                interval_seconds=settings.runtime_reap_interval,
                stop_after_seconds=settings.runtime_idle_stop_seconds,
                delete_after_seconds=settings.runtime_idle_delete_seconds,
                purge_after_seconds=settings.runtime_idle_purge_seconds,
            )
            reaper_task = asyncio.create_task(reaper.run(reaper_stop))
            LOG.info(
                "runtime reaper started: stop=%ss delete=%ss purge=%ss",
                settings.runtime_idle_stop_seconds,
                settings.runtime_idle_delete_seconds,
                settings.runtime_idle_purge_seconds,
            )
        try:
            yield
        finally:
            if reaper_stop is not None:
                reaper_stop.set()
            memory_stop.set()
            if memory_task is not None:
                memory_task.cancel()
                try:
                    await memory_task
                except (asyncio.CancelledError, Exception):
                    pass
            if reaper_task is not None:
                reaper_task.cancel()
                try:
                    await reaper_task
                except (asyncio.CancelledError, Exception):
                    pass
            tools.close()
            if container_pool is not None:
                container_pool.close()

    app = FastAPI(
        title=APP_NAME,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.audit = audit
    app.state.registry = registry
    app.state.kb = registry.get(registry.infos[0].id)
    app.state.sandbox = sandbox
    app.state.container_runtime = container_runtime
    app.state.container_pool = container_pool
    app.state.workspace_service = workspace_service
    app.state.active_runs = active_runs
    app.state.gate = gate
    app.state.user_memory = memory_store
    app.state.runner = runner
    app.state.kernel = kernel
    app.state.tools = tools
    app.state.connector_service = connector_service
    app.state.plugin_host = plugin_host
    app.state.job_queue = job_queue

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method in {"POST", "PATCH", "DELETE", "PUT"}:
            origin = request.headers.get("origin")
            if origin:
                host = request.headers.get("host", "")
                if urlparse(origin).netloc != host:
                    return JSONResponse({"detail": "跨站请求已拒绝"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self' data:; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        return response

    # ------------------------------------------------------------ auth deps

    def current_user(request: Request) -> dict[str, Any]:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(status_code=401, detail="请先登录")
        db = connect(settings.db_path)
        try:
            user = repo.session_user(db, token_digest(token))
        finally:
            db.close()
        if user is None:
            raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
        request.state.user_id = user["id"]
        user_id_var.set(user["id"])
        return user

    User = Depends(current_user)

    def resolve_kb(user: dict[str, Any], kb_id: str | None = None) -> KnowledgeService:
        allowed = registry.for_user(user)
        if not allowed:
            raise HTTPException(status_code=403, detail="当前账号还没有可访问的知识库")
        if not kb_id:
            return allowed[0]
        for kb_service in allowed:
            if kb_service.info.id == kb_id:
                return kb_service
        raise HTTPException(status_code=403, detail="没有这个知识库的访问权限")

    def memory_payload(user: dict[str, Any]) -> list[dict[str, Any]]:
        memories = memory_store.list(user["id"], user=user)
        ids = [m.get("source_conversation_id") for m in memories if m.get("source_conversation_id")]
        titles: dict[str, str] = {}
        if ids:
            db = connect(settings.db_path)
            try:
                marks = ",".join("?" for _ in ids)
                rows = db.execute(
                    f"SELECT id, title FROM conversations WHERE user_id=? AND id IN ({marks})",
                    (user["id"], *ids),
                ).fetchall()
                titles = {row["id"]: row["title"] for row in rows}
            finally:
                db.close()
        for memory in memories:
            source = memory.get("source_conversation_id") or ""
            memory["source_conversation_title"] = titles.get(source, "")
        return memories

    # ---------------------------------------------------------------- meta

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        default_service = registry.get(registry.infos[0].id)
        return {
            "name": APP_NAME,
            "version": "0.1.0",
            "agent_enabled": settings.agent_enabled,
            "agent_engine": "external" if settings.agent_enabled else "local",
            "sandbox": settings.sandbox,
            "apple_network": {
                "configured": settings.apple_container_network,
                "effective": network_policy(settings.apple_container_network)[0] or "default",
                "isolated": network_policy(settings.apple_container_network)[1],
            },
            "departments": registry.departments,
            "default_department": registry.default_department,
            "knowledge_bases": registry.catalog_public(),
            "tools": tools.summary(),
            "plugins": plugin_host.summary(),
            "concurrency": gate.metrics().public(),
            "queue": {"enabled": job_queue is not None, "name": settings.queue_name},
            "apple_pool": container_pool.status() if container_pool is not None else {"enabled": False},
            "kb": {
                "provider": default_service.provider.kind,
                "chat_model": getattr(
                    default_service.provider.chat,
                    "chat_model",
                    getattr(default_service.provider.chat, "model", "unknown"),
                ),
                "warning": default_service.warning,
                "corpus": default_service.corpus,
            },
            "demo_accounts": DEMO_ACCOUNTS if settings.seed_demo_users else [],
        }

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "knowledge_bases": {
                svc.info.id: svc.corpus for svc in registry.all()
            },
            "agent": "external" if settings.agent_enabled else "local",
            "sandbox": settings.sandbox,
            "apple_network": {
                "configured": settings.apple_container_network,
                "effective": network_policy(settings.apple_container_network)[0] or "default",
                "isolated": network_policy(settings.apple_container_network)[1],
            },
        }

    # ---------------------------------------------------------------- auth

    @app.post("/api/auth/register")
    def register(payload: RegisterIn, response: Response) -> dict[str, Any]:
        username = _clean_username(payload.username)
        display_name = (payload.display_name or username).strip()[:30] or username
        department = (payload.department or registry.default_department).strip()
        if department not in registry.departments:
            raise HTTPException(status_code=400, detail="请选择有效的部门")
        db = connect(settings.db_path)
        try:
            if repo.find_user_by_username(db, username) is not None:
                raise HTTPException(status_code=409, detail="这个用户名已被占用")
            try:
                user = repo.create_user(
                    db,
                    username=username,
                    display_name=display_name,
                    password_hash=hash_password(payload.password),
                    department=department,
                )
            except Exception as exc:  # unique violation across SQLite/Postgres
                if not is_integrity_error(exc):
                    raise
                raise HTTPException(status_code=409, detail="这个用户名已被占用") from exc
            token = _new_session(db, user["id"], settings)
        finally:
            db.close()
        _set_cookie(response, token, settings)
        return {"user": user}

    @app.post("/api/auth/login")
    def login(payload: LoginIn, response: Response) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            row = repo.find_user_by_username(db, payload.username.strip())
            if row is None or not verify_password(payload.password, row["password_hash"]):
                raise HTTPException(status_code=401, detail="用户名或密码不正确")
            user = repo.user_public(row)
            token = _new_session(db, user["id"], settings)
        finally:
            db.close()
        _set_cookie(response, token, settings)
        return {"user": user}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, Any]:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            db = connect(settings.db_path)
            try:
                repo.delete_session(db, token_digest(token))
            finally:
                db.close()
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(user: dict[str, Any] = User) -> dict[str, Any]:
        return {"user": user}

    # ------------------------------------------------------ knowledge bases

    @app.get("/api/kbs")
    def knowledge_bases(user: dict[str, Any] = User) -> dict[str, Any]:
        allowed = registry.for_user(user)
        default = allowed[0] if allowed else None
        return {
            "knowledge_bases": registry.catalog(user),
            "default": default.info.id if default else None,
            "departments": registry.departments,
        }

    def can_write_memory_scope(user: dict[str, Any], scope: str, scope_id: str) -> bool:
        role = str(user.get("role") or "member")
        if scope == "personal":
            return True
        if scope == "org":
            return True
        return role == "admin" or scope_id == str(user.get("department") or "")

    @app.get("/api/memories")
    def list_memories(user: dict[str, Any] = User) -> dict[str, Any]:
        return {
            "memories": memory_payload(user),
            "stats": memory_store.stats(user["id"], user=user),
        }

    @app.post("/api/memories")
    def add_memory(payload: MemoryIn, user: dict[str, Any] = User) -> dict[str, Any]:
        scope_id = payload.scope_id or (str(user.get("department") or "") if payload.scope == "department" else "org")
        if not can_write_memory_scope(user, payload.scope, scope_id):
            raise HTTPException(status_code=403, detail="没有写入该范围记忆的权限")
        try:
            result = memory_store.remember(
                user["id"],
                kind=payload.kind,
                content=payload.content,
                subject=payload.subject,
                predicate=payload.predicate,
                object_text=payload.object_text,
                importance=payload.importance,
                confidence=payload.confidence,
                scope=payload.scope,
                scope_id=scope_id,
                agent_id=payload.agent_id,
                source_uri="manual",
                evidence_snippet=payload.content,
            )
            audit.record(
                event_type="memory_write",
                component="memory",
                action=payload.scope,
                status="ok",
                metadata={
                    "op": "create" if result.get("status") in {"created", "superseded"} else "noop",
                    "result": result.get("status"),
                    "memory_id": result.get("id", ""),
                    "snapshot": result.get("snapshot"),
                    "superseded_snapshots": result.get("superseded_snapshots") or [],
                },
                user_id=user["id"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "memory": result,
            "memories": memory_payload(user),
            "stats": memory_store.stats(user["id"], user=user),
        }

    @app.post("/api/memories/consolidate")
    def consolidate_memories(
        expire_days: int = Query(default=0, ge=0, le=3650),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        result = memory_store.consolidate(user["id"], expire_days=expire_days)
        return {
            "result": result,
            "memories": memory_payload(user),
            "stats": memory_store.stats(user["id"], user=user),
        }

    @app.get("/api/team/memories")
    def team_memories(
        scope: str = Query(default="", max_length=20),
        status: str = Query(default="", max_length=20),
        agent_id: str = Query(default="", max_length=80),
        limit: int = Query(default=100, ge=1, le=300),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        if user.get("role") not in {"admin", "manager"}:
            raise HTTPException(status_code=403, detail="需要团队管理权限")
        rows = memory_store.team(
            user=user, scope=scope, status=status, agent_id=agent_id, limit=limit
        )
        counts: dict[str, int] = {}
        for row in rows:
            key = f"{row.get('scope', '')}:{row.get('status', '')}"
            counts[key] = counts.get(key, 0) + 1
        return {"memories": rows, "counts": counts}

    @app.get("/api/memories/pending")
    def pending_memories(user: dict[str, Any] = User) -> dict[str, Any]:
        return {"memories": memory_store.pending(user=user), "can_approve": user.get("role") in {"admin", "manager"}}

    @app.post("/api/memories/{memory_id}/approve")
    def approve_memory(memory_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        try:
            result = memory_store.approve(memory_id, approver=user["id"], user=user)
        except MemoryConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result is None:
            raise HTTPException(status_code=403, detail="没有审批权限或状态不允许")
        audit.record(
            event_type="memory_approve",
            component="memory",
            action="approve",
            status="ok",
            metadata={"op": "approve", "previous_status": "pending", "snapshot": result},
            user_id=user["id"],
        )
        return {"memory": result, "stats": memory_store.stats(user["id"], user=user)}

    @app.post("/api/memories/{memory_id}/reject")
    def reject_memory(memory_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        result = memory_store.reject(memory_id, approver=user["id"], user=user)
        if result is None:
            raise HTTPException(status_code=403, detail="没有审批权限或状态不允许")
        audit.record(
            event_type="memory_reject",
            component="memory",
            action="reject",
            status="ok",
            metadata={"op": "reject", "previous_status": "pending", "snapshot": result},
            user_id=user["id"],
        )
        return {"memory": result, "stats": memory_store.stats(user["id"], user=user)}

    @app.delete("/api/memories/{memory_id}")
    def delete_memory(memory_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        snapshot = memory_store.snapshot(memory_id)
        if snapshot is None or not memory_store.forget(user["id"], memory_id, user=user):
            raise HTTPException(status_code=404, detail="记忆不存在")
        audit.record(
            event_type="memory_delete",
            component="memory",
            action="forget",
            status="ok",
            metadata={"op": "delete", "memory_id": memory_id, "snapshot": snapshot},
            user_id=user["id"],
        )
        return {"ok": True}

    @app.post("/api/jobs/agent/{conversation_id}")
    def enqueue_agent_job_for_conversation(
        conversation_id: str, payload: JobIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        if job_queue is None:
            raise HTTPException(
                status_code=503,
                detail="Worker 队列未启用；请配置 WEBCHAT_QUEUE_URL",
            )
        content = payload.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="消息不能为空")
        db = connect(settings.db_path)
        try:
            conversation = repo.get_conversation(
                db, conversation_id=conversation_id, user_id=user["id"]
            )
            if conversation is None:
                raise HTTPException(status_code=404, detail="会话不存在")
            service = resolve_kb(user, conversation["kb_id"])
            imported_agent = None
            if payload.agent_id and payload.mode == "agent":
                imported_agent = get_agent(db, payload.agent_id, user)
                if imported_agent is None:
                    raise HTTPException(status_code=404, detail="Agent 不存在或不属于你")
            user_message = repo.add_message(
                db,
                conversation_id=conversation_id,
                user_id=user["id"],
                role="user",
                content=content,
                meta={"mode": payload.mode, "agent_id": payload.agent_id, "queued": True},
            )
            repo.auto_title_if_needed(
                db,
                conversation_id=conversation_id,
                user_id=user["id"],
                first_user_text=content,
            )
        finally:
            db.close()
        job = new_job(
            user["id"],
            conversation_id,
            kind="agent_run",
            payload={
                "content": content,
                "mode": payload.mode,
                "agent_id": payload.agent_id or "",
                "user_message_id": user_message["id"],
                "kb_id": service.info.id,
            },
        )
        job_queue.enqueue(job)
        return {"job": job.public(), "user_message": user_message}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        if job_queue is None:
            raise HTTPException(status_code=503, detail="Worker 队列未启用")
        job = job_queue.get(job_id)
        if job is None or job.user_id != user["id"]:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"job": job.public()}

    @app.get("/api/containers/pool")
    def container_pool_status(user: dict[str, Any] = User) -> dict[str, Any]:
        if container_pool is None:
            return {"enabled": False}
        return container_pool.status()

    @app.get("/api/connectors/catalog")
    def connector_catalog(user: dict[str, Any] = User) -> dict[str, Any]:
        from .connectors import KNOWN_CONNECTORS
        return {"connectors": KNOWN_CONNECTORS}

    @app.get("/api/connectors")
    def list_connectors(user: dict[str, Any] = User) -> dict[str, Any]:
        return {"connectors": connector_service.store.list(user["id"])}

    @app.post("/api/connectors/propose")
    def propose_connector(
        payload: ConnectorProposeIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        known = known_for(payload.query)
        if known:
            return {"source": "known", "specs": known}
        default_service = registry.get(registry.infos[0].id)
        try:
            spec = propose_with_llm(default_service.provider.chat, payload.query)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"模型生成连接器失败：{exc}") from exc
        return {"source": "llm", "spec": spec}

    @app.post("/api/connectors")
    def create_connector(payload: ConnectorIn, user: dict[str, Any] = User) -> dict[str, Any]:
        try:
            spec = parse_spec(payload.spec)
            connector = connector_service.store.create(user["id"], spec, source="draft")
        except ConnectorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"connector": connector}

    @app.post("/api/connectors/{connector_id}/test")
    def test_connector(
        connector_id: str,
        payload: ConnectorTestIn,
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        connector = connector_service.store.get(connector_id)
        if connector is None or connector["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="连接器不存在")
        try:
            spec = parse_spec(connector["spec"])
            result = connector_service.executor.execute(spec, payload.arguments)
        except (ConnectorError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"result": result}

    @app.post("/api/connectors/{connector_id}/approve")
    def approve_connector(
        connector_id: str, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        connector = connector_service.store.get(connector_id)
        if connector is None or connector["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="连接器不存在")
        try:
            spec = parse_spec(connector["spec"])
            connector_service.register(spec)
        except ConnectorError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        updated = connector_service.store.approve(connector_id, user["id"])
        return {"connector": updated, "tool": spec.tool_name}

    @app.post("/api/connectors/{connector_id}/reject")
    def reject_connector(
        connector_id: str, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        connector = connector_service.store.get(connector_id)
        if connector is None or connector["user_id"] != user["id"]:
            raise HTTPException(status_code=404, detail="连接器不存在")
        return {"connector": connector_service.store.reject(connector_id, user["id"])}

    @app.get("/api/audit")
    def audit_events(
        limit: int = Query(default=50, ge=1, le=200),
        event_type: str = Query(default="", max_length=60),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        return {
            "events": audit.query(
                user_id=user["id"], limit=limit, event_type=event_type
            )
        }

    @app.post("/api/audit/{event_id}/undo")
    def undo_audit_event(event_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        event = audit.get(event_id, user_id=user["id"])
        if event is None:
            raise HTTPException(status_code=404, detail="审计事件不存在")
        if event.get("component") != "memory":
            raise HTTPException(status_code=400, detail="该审计事件不可撤销")
        result = memory_store.undo_event(event.get("metadata") or {})
        if result is None:
            raise HTTPException(status_code=400, detail="该事件没有可回滚快照")
        audit.record(
            event_type="memory_rollback",
            component="memory",
            action="undo",
            status="ok",
            metadata={"source_event": event_id, **result},
            user_id=user["id"],
        )
        return {"ok": True, "result": result, "stats": memory_store.stats(user["id"], user=user)}

    @app.get("/api/metrics")
    def metrics(user: dict[str, Any] = User) -> dict[str, Any]:
        return audit.metrics(user_id=user["id"])

    def require_admin(user: dict[str, Any]) -> None:
        if user.get("role") != "admin":
            raise HTTPException(status_code=403, detail="需要管理员权限")

    @app.get("/api/admin/users")
    def admin_users(user: dict[str, Any] = User) -> dict[str, Any]:
        require_admin(user)
        db = connect(settings.db_path)
        try:
            rows = db.execute("SELECT * FROM users ORDER BY department, username").fetchall()
            return {"users": [repo.user_public(row) for row in rows]}
        finally:
            db.close()

    @app.patch("/api/admin/users/{user_id}")
    def admin_patch_user(
        user_id: str, payload: AdminUserPatch, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        require_admin(user)
        if payload.department and payload.department not in registry.departments:
            raise HTTPException(status_code=400, detail="部门不存在")
        db = connect(settings.db_path)
        try:
            target = repo.get_user(db, user_id)
            if target is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            repo.update_user_role_department(
                db,
                user_id,
                role=payload.role or (target["role"] if "role" in target.keys() else "member"),
                department=payload.department
                or (target["department"] if "department" in target.keys() else "产品部"),
            )
            return {"user": repo.user_public(repo.get_user(db, user_id))}
        finally:
            db.close()

    # ------------------------------------------------------------- runtimes

    @app.get("/api/runtimes/me")
    def my_runtime(user: dict[str, Any] = User) -> dict[str, Any]:
        return {"mapping": workspace_service.get(user["id"])}

    @app.post("/api/runtimes/me/ensure")
    def ensure_my_runtime(
        payload: RuntimeEnsureIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        try:
            if payload.conversation_id:
                db = connect(settings.db_path)
                try:
                    conversation = repo.get_conversation(
                        db,
                        conversation_id=payload.conversation_id,
                        user_id=user["id"],
                    )
                finally:
                    db.close()
                if conversation is None:
                    raise HTTPException(status_code=404, detail="会话不存在")
                mapping = workspace_service.ensure_workspace(
                    user["id"], payload.conversation_id
                )
            else:
                mapping = workspace_service.ensure_user(user["id"])
        except ContainerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"mapping": mapping, "state": workspace_service.get(user["id"])}

    @app.post("/api/runtimes/me/restore")
    def restore_my_runtime(
        payload: RuntimeRestoreIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            conversation = repo.get_conversation(
                db, conversation_id=payload.conversation_id, user_id=user["id"]
            )
        finally:
            db.close()
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        try:
            restored = workspace_service.restore(user["id"], payload.conversation_id)
        except ContainerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return restored

    @app.post("/api/runtimes/me/stop")
    def stop_my_runtime(user: dict[str, Any] = User) -> dict[str, Any]:
        try:
            mapping = workspace_service.stop(user["id"])
        except ContainerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"mapping": mapping}

    @app.delete("/api/runtimes/me")
    def delete_my_runtime(
        delete_volume: bool = Query(default=False), user: dict[str, Any] = User
    ) -> dict[str, Any]:
        try:
            workspace_service.delete(user["id"], delete_volume=delete_volume)
        except ContainerRuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/plugins")
    def list_plugins(user: dict[str, Any] = User) -> dict[str, Any]:
        return plugin_host.summary()

    # --------------------------------------------------------------- tools

    @app.get("/api/tools")
    def list_tools(user: dict[str, Any] = User) -> dict[str, Any]:
        return tools.summary()

    @app.post("/api/tools/{name:path}/call")
    def call_tool(
        name: str, payload: ToolCallIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        try:
            result = tools.call(name, payload.arguments)
        except ToolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"result": result.public()}

    # -------------------------------------------------------------- agents

    @app.get("/api/agents")
    def agents(user: dict[str, Any] = User) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            return {"agents": list_agents(db, user)}
        finally:
            db.close()

    @app.post("/api/agents")
    def create_agent(payload: AgentIn, user: dict[str, Any] = User) -> dict[str, Any]:
        try:
            record = build_agent(
                user_id=user["id"],
                markdown=payload.markdown,
                files=[{"name": f.name, "content": f.content} for f in payload.files],
                name=payload.name,
                description=payload.description,
                kind=payload.kind,
                entrypoint_override=payload.entrypoint,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db = connect(settings.db_path)
        try:
            return {"agent": save_agent(db, record)}
        finally:
            db.close()

    @app.post("/api/agents/import/github")
    def import_agent_from_github(
        payload: GitHubImportIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        try:
            imported = import_from_github(
                payload.url,
                ref=payload.ref,
                path=payload.path,
                entrypoint=payload.entrypoint,
            )
        except GitHubImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            record = build_agent(
                user_id=user["id"],
                markdown=imported.markdown,
                files=imported.files,
                name=payload.name or imported.name,
                description=payload.description or imported.description,
                kind=payload.kind or imported.kind,
                source_url=imported.source_url,
                source_ref=imported.source_ref,
                source_path=imported.source_path,
                entrypoint_override=payload.entrypoint or imported.entrypoint,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db = connect(settings.db_path)
        try:
            return {"agent": save_agent(db, record)}
        finally:
            db.close()

    @app.get("/api/agent-runs/{run_id}/artifacts/{name:path}")
    def download_agent_artifact(
        run_id: str, name: str, user: dict[str, Any] = User
    ) -> FileResponse:
        db = connect(settings.db_path)
        try:
            if not owns_run(db, run_id, user["id"]):
                raise HTTPException(status_code=404, detail="产物不存在")
        finally:
            db.close()
        path = build_artifact_store(settings).path_for(run_id, name)
        if path is None:
            raise HTTPException(status_code=404, detail="产物不存在")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=name.rsplit("/", 1)[-1],
        )

    @app.patch("/api/agents/{agent_id}")
    def update_agent_config(
        agent_id: str, payload: AgentConfigIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        from .agent_defs import RUNTIME_BY_EXT, _clean_relpath, row_to_agent

        db = connect(settings.db_path)
        try:
            agent = get_agent(db, agent_id, user)
            if agent is None:
                raise HTTPException(status_code=404, detail="Agent 不存在")
            if payload.entrypoint is None:
                entrypoint = agent.entrypoint
            else:
                raw = payload.entrypoint.strip().replace("\\", "/")
                if not raw:
                    entrypoint = ""
                else:
                    try:
                        entrypoint = _clean_relpath(raw, executable=True)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                    if entrypoint not in {f["name"] for f in agent.files}:
                        raise HTTPException(status_code=400, detail="入口脚本不在该 Agent 的文件中")
            runtime = payload.runtime or (
                RUNTIME_BY_EXT.get(Path(entrypoint).suffix.lower(), agent.runtime)
                if entrypoint
                else agent.runtime
            )
            if runtime not in {"python", "bash", "node"}:
                raise HTTPException(status_code=400, detail="runtime 只能是 python / bash / node")
            timeout = payload.timeout if payload.timeout is not None else agent.timeout
            with db:
                db.execute(
                    "UPDATE agents SET entrypoint=?, runtime=?, timeout=? WHERE id=? AND user_id=?",
                    (entrypoint, runtime, float(timeout), agent_id, user["id"]),
                )
            row = db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            return {"agent": row_to_agent(row).public()}
        finally:
            db.close()

    @app.post("/api/agents/{agent_id}/test")
    def test_agent(
        agent_id: str, payload: AgentTestIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            agent = get_agent(db, agent_id, user)
            if agent is None:
                raise HTTPException(status_code=404, detail="Agent 不存在")
        finally:
            db.close()

        checks: list[dict[str, Any]] = []
        if not agent.entrypoint:
            checks = [
                {"id": "entrypoint", "label": "入口脚本", "ok": False, "detail": "未配置 entrypoint"},
                {
                    "id": "runtime",
                    "label": "本地命令",
                    "ok": False,
                    "detail": "Prompt Skill 不执行本地命令，需要真实 LLM",
                },
                {"id": "run", "label": "执行", "ok": False, "detail": "未执行"},
                {"id": "output", "label": "输出", "ok": False, "detail": "无 stdout"},
                {"id": "artifacts", "label": "产物下载", "ok": False, "detail": "无产物"},
            ]
            return {
                "status": "prompt",
                "agent": agent.public(),
                "checks": checks,
                "answer": "这是一个 Prompt Skill，没有入口脚本；请在 Agent 模式中使用，或先配置 entrypoint。",
                "trace": [],
                "execution": None,
                "artifacts": [],
            }

        command_ok = True
        command_detail = ""
        if agent.runtime == "python":
            command_detail = sys.executable
            command_ok = bool(sys.executable)
        elif agent.runtime == "bash":
            command_detail = "/bin/bash"
            command_ok = Path("/bin/bash").exists()
        else:
            node = shutil.which("node")
            command_detail = node or "node"
            command_ok = bool(node)
        checks.append(
            {"id": "entrypoint", "label": "入口脚本", "ok": True, "detail": agent.entrypoint}
        )
        checks.append(
            {"id": "runtime", "label": "本地命令", "ok": command_ok, "detail": command_detail}
        )
        if not settings.allow_script_exec:
            checks += [
                {"id": "run", "label": "执行", "ok": False, "detail": "服务端禁用了脚本执行"},
                {"id": "output", "label": "输出", "ok": False, "detail": "未执行"},
                {"id": "artifacts", "label": "产物下载", "ok": False, "detail": "无产物"},
            ]
            return {
                "status": "disabled",
                "agent": agent.public(),
                "checks": checks,
                "answer": "服务端已禁用脚本执行（WEBCHAT_ALLOW_SCRIPT_EXEC=0）。",
                "trace": [],
                "execution": None,
                "artifacts": [],
            }
        if not command_ok:
            checks += [
                {"id": "run", "label": "执行", "ok": False, "detail": "runtime 不可用"},
                {"id": "output", "label": "输出", "ok": False, "detail": "未执行"},
                {"id": "artifacts", "label": "产物下载", "ok": False, "detail": "无产物"},
            ]
            return {
                "status": "runtime_missing",
                "agent": agent.public(),
                "checks": checks,
                "answer": "当前环境缺少该 runtime 命令，无法执行。",
                "trace": [],
                "execution": None,
                "artifacts": [],
            }

        service = resolve_kb(user)
        conversation = {"id": "test", "kb_id": service.info.id, "mode": "agent"}
        try:
            result = runner.run(
                question=payload.question,
                mode="agent",
                user=user,
                conversation=conversation,
                history=[],
                kb=service,
                imported_agent=agent,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "agent": agent.public(),
                "checks": checks
                + [
                    {"id": "run", "label": "执行", "ok": False, "detail": type(exc).__name__},
                    {"id": "output", "label": "输出", "ok": False, "detail": "异常"},
                    {"id": "artifacts", "label": "产物下载", "ok": False, "detail": "无产物"},
                ],
                "answer": f"测试异常：{type(exc).__name__}: {exc}",
                "trace": [],
                "execution": None,
                "artifacts": [],
            }

        execution = (result.meta or {}).get("execution") or {}
        run_id = execution.get("run_id")
        artifacts = execution.get("artifacts") or []
        if run_id:
            wdb = connect(settings.db_path)
            try:
                record_run(
                    wdb,
                    run_id=run_id,
                    user_id=user["id"],
                    agent_id=agent.id,
                    agent_name=agent.name,
                    execution=execution,
                )
            finally:
                wdb.close()
        for artifact in artifacts:
            name = artifact.get("name", "")
            artifact["url"] = (
                f"/api/agent-runs/{run_id}/artifacts/"
                + "/".join(quote(str(part)) for part in name.split("/"))
            )
        ran = bool(execution.get("ran"))
        exit_code = execution.get("exit_code")
        ok = ran and exit_code in (0, None)
        checks.append(
            {
                "id": "run",
                "label": "执行",
                "ok": ok,
                "detail": f"exit={exit_code}" if ran else execution.get("reason", "未执行"),
            }
        )
        checks.append(
            {
                "id": "output",
                "label": "输出",
                "ok": bool((execution.get("stdout") or "").strip()),
                "detail": f"{len(execution.get('stdout') or '')} 字符",
            }
        )
        checks.append(
            {
                "id": "artifacts",
                "label": "产物下载",
                "ok": bool(artifacts),
                "detail": f"{len(artifacts)} 个文件" if artifacts else "无产物",
            }
        )
        return {
            "status": "ok" if ok else "failed",
            "agent": agent.public(),
            "checks": checks,
            "answer": result.answer,
            "trace": result.trace,
            "execution": execution,
            "artifacts": artifacts,
        }

    @app.get("/api/agents/{agent_id}")
    def get_agent_definition(
        agent_id: str, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            data = agent_detail(db, agent_id, user)
            if data is None:
                raise HTTPException(status_code=404, detail="Agent 不存在")
            return {"agent": data}
        finally:
            db.close()

    @app.delete("/api/agents/{agent_id}")
    def remove_agent(agent_id: str, user: dict[str, Any] = User) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            if not delete_agent(db, agent_id, user["id"]):
                raise HTTPException(status_code=404, detail="Agent 不存在")
            return {"ok": True}
        finally:
            db.close()

    # ------------------------------------------------------- conversations

    @app.get("/api/conversations")
    def conversations(
        kb: str = Query(default="", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        service = resolve_kb(user, kb)
        db = connect(settings.db_path)
        try:
            return {
                "knowledge_base": service.info.id,
                "conversations": repo.list_conversations(
                    db, user["id"], kb_id=service.info.id
                ),
            }
        finally:
            db.close()

    @app.post("/api/conversations")
    def new_conversation(
        payload: ConversationIn, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        service = resolve_kb(user, payload.kb)
        db = connect(settings.db_path)
        try:
            return {
                "conversation": repo.create_conversation(
                    db,
                    user_id=user["id"],
                    mode=payload.mode,
                    kb_id=service.info.id,
                )
            }
        finally:
            db.close()

    @app.get("/api/conversations/{conversation_id}")
    def get_conversation(
        conversation_id: str, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            conversation = repo.get_conversation(
                db, conversation_id=conversation_id, user_id=user["id"]
            )
            if conversation is None:
                raise HTTPException(status_code=404, detail="会话不存在")
            # A conversation belongs to exactly one KB; access can change with
            # department, so re-check it on every load.
            resolve_kb(user, conversation["kb_id"])
            messages = repo.list_messages(
                db, conversation_id=conversation_id, user_id=user["id"]
            )
            return {"conversation": conversation, "messages": messages}
        finally:
            db.close()

    @app.patch("/api/conversations/{conversation_id}")
    def patch_conversation(
        conversation_id: str, payload: ConversationPatch, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        title = " ".join(payload.title.split()).strip()
        if not title:
            raise HTTPException(status_code=400, detail="标题不能为空")
        db = connect(settings.db_path)
        try:
            ok = repo.rename_conversation(
                db, conversation_id=conversation_id, user_id=user["id"], title=title[:40]
            )
            if not ok:
                raise HTTPException(status_code=404, detail="会话不存在")
            conversation = repo.get_conversation(
                db, conversation_id=conversation_id, user_id=user["id"]
            )
            return {"conversation": conversation}
        finally:
            db.close()

    @app.delete("/api/conversations/{conversation_id}")
    def remove_conversation(
        conversation_id: str, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            if not repo.delete_conversation(
                db, conversation_id=conversation_id, user_id=user["id"]
            ):
                raise HTTPException(status_code=404, detail="会话不存在")
            return {"ok": True}
        finally:
            db.close()

    @app.post("/api/conversations/{conversation_id}/cancel")
    def cancel_conversation(
        conversation_id: str, user: dict[str, Any] = User
    ) -> dict[str, Any]:
        db = connect(settings.db_path)
        try:
            conversation = repo.get_conversation(
                db, conversation_id=conversation_id, user_id=user["id"]
            )
        finally:
            db.close()
        if conversation is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"cancelled": active_runs.cancel(conversation_id)}

    # ---------------------------------------------------------- chat (SSE)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_message(
        conversation_id: str,
        payload: MessageIn,
        request: Request,
        user: dict[str, Any] = User,
    ) -> StreamingResponse:
        content = payload.content.strip()
        if not content:
            raise HTTPException(status_code=400, detail="消息不能为空")
        try:
            lease = await gate.acquire(user["id"])
        except QueueFullError as exc:
            raise HTTPException(
                status_code=429,
                detail=f"当前请求排队已满，请稍后再试（{exc}）",
                headers={"Retry-After": "1"},
            ) from exc
        except QueueTimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="当前请求排队超时，请稍后再试",
                headers={"Retry-After": "3"},
            ) from exc
        try:
            db = connect(settings.db_path)
            try:
                conversation = repo.get_conversation(
                    db, conversation_id=conversation_id, user_id=user["id"]
                )
                if conversation is None:
                    raise HTTPException(status_code=404, detail="会话不存在")
                service = resolve_kb(user, conversation["kb_id"])
                imported_agent = None
                if payload.agent_id and payload.mode == "agent":
                    imported_agent = get_agent(db, payload.agent_id, user)
                    if imported_agent is None:
                        raise HTTPException(status_code=404, detail="Agent 不存在或不属于你")
                if (
                    imported_agent is not None
                    and settings.sandbox.startswith("apple")
                    and settings.runtime_auto_ensure
                ):
                    try:
                        workspace_service.ensure_workspace(user["id"], conversation_id)
                    except Exception as exc:  # noqa: BLE001 - runtime is optional
                        LOG.warning("container workspace ensure failed: %s", exc)
                history = repo.conversation_context(
                    db, conversation_id=conversation_id, user_id=user["id"], limit=8
                )
                user_message = repo.add_message(
                    db,
                    conversation_id=conversation_id,
                    user_id=user["id"],
                    role="user",
                    content=content,
                    meta={"mode": payload.mode, "agent_id": payload.agent_id, "live": payload.live},
                )
                title = repo.auto_title_if_needed(
                    db,
                    conversation_id=conversation_id,
                    user_id=user["id"],
                    first_user_text=content,
                )
                try:
                    extracted = memory_store.extract_and_remember(
                        user["id"],
                        content,
                        conversation_id=conversation_id,
                        message_id=user_message["id"],
                        agent_id=str(payload.agent_id or ""),
                    )
                    for item in extracted:
                        audit.record(
                            event_type="memory_write",
                            component="memory",
                            action="auto",
                            status="ok" if item.get("status") != "skipped" else "skipped",
                            metadata={
                                "op": "create" if item.get("status") in {"created", "superseded"} else "noop",
                                "result": item.get("status", ""),
                                "memory_id": item.get("id", ""),
                                "snapshot": item.get("snapshot"),
                                "superseded_snapshots": item.get("superseded_snapshots") or [],
                            },
                            user_id=user["id"],
                            conversation_id=conversation_id,
                        )
                except Exception as exc:  # noqa: BLE001 - memory is optional
                    LOG.warning("memory extract failed: %s", exc)
            finally:
                db.close()
        except Exception:
            lease.release()
            raise

        async def event_stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
            loop = asyncio.get_running_loop()
            cancel_token = active_runs.begin(conversation_id)

            def emit(event: str, data: Any) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, (event, data))

            def work() -> None:
                run_started = time.perf_counter()
                def status(title_text: str, detail: str) -> None:
                    emit("status", {"title": title_text, "detail": detail})

                try:
                    result = kernel.run(
                        question=content,
                        mode=payload.mode,
                        user=user,
                        conversation=conversation,
                        history=history,
                        kb=service,
                        imported_agent=imported_agent,
                        on_status=status,
                        live=payload.live,
                        cancel_token=cancel_token,
                    )
                    wdb = connect(settings.db_path)
                    try:
                        meta = result.message_meta()
                        meta["request_id"] = getattr(request.state, "request_id", "")
                        execution = (meta or {}).get("execution") or {}
                        if execution.get("run_id"):
                            record_run(
                                wdb,
                                run_id=execution["run_id"],
                                user_id=user["id"],
                                agent_id=str(meta.get("agent_id") or ""),
                                agent_name=str(meta.get("agent_name") or ""),
                                execution=execution,
                            )
                        message = repo.add_message(
                            wdb,
                            conversation_id=conversation_id,
                            user_id=user["id"],
                            role="assistant",
                            content=result.answer,
                            citations=result.citations,
                            meta=meta,
                        )
                        fresh = repo.get_conversation(
                            wdb, conversation_id=conversation_id, user_id=user["id"]
                        )
                        if imported_agent is not None and settings.sandbox.startswith("apple"):
                            try:
                                execution = meta.get("execution") or {}
                                workspace_service.save_checkpoint(
                                    user["id"],
                                    conversation_id,
                                    last_message_id=message["id"],
                                    last_run_id=str(execution.get("run_id") or ""),
                                    state={
                                        "answer_mode": meta.get("answer_mode", ""),
                                        "agent_name": meta.get("agent_name", ""),
                                        "artifacts": execution.get("artifacts") or [],
                                        "trace_steps": len(meta.get("trace") or []),
                                    },
                                )
                                workspace_service.touch_workspace(user["id"], conversation_id)
                            except Exception as exc:  # noqa: BLE001
                                LOG.warning("checkpoint save failed: %s", exc)
                    finally:
                        wdb.close()
                    audit.record(
                        event_type="chat_run",
                        component="agent",
                        action=str(payload.mode),
                        status="ok",
                        duration_ms=int((time.perf_counter() - run_started) * 1000),
                        metadata={
                            "answer_mode": meta.get("answer_mode", ""),
                            "tools_used": meta.get("tools_used") or [],
                            "tool_steps": meta.get("tool_steps", 0),
                            "agent_id": meta.get("agent_id", ""),
                        },
                        user_id=user["id"],
                        conversation_id=conversation_id,
                        run_id=str((meta.get("execution") or {}).get("run_id") or ""),
                        request_id=getattr(request.state, "request_id", ""),
                    )
                    emit(
                        "result",
                        {
                            "message": message,
                            "conversation": fresh or conversation,
                            "user_message": user_message,
                        },
                    )
                except RunCancelled:
                    audit.record(
                        event_type="chat_run",
                        component="agent",
                        action=str(payload.mode),
                        status="cancelled",
                        duration_ms=int((time.perf_counter() - run_started) * 1000),
                        failure_class="cancelled",
                        user_id=user["id"],
                        conversation_id=conversation_id,
                        request_id=getattr(request.state, "request_id", ""),
                    )
                    try:
                        cdb = connect(settings.db_path)
                        try:
                            message = repo.add_message(
                                cdb,
                                conversation_id=conversation_id,
                                user_id=user["id"],
                                role="assistant",
                                content=str(exc) or "本次生成已取消。",
                                meta={
                                    "mode": payload.mode,
                                    "engine": "local",
                                    "answer_mode": "cancelled",
                                    "failure": {"kind": "cancelled", "retryable": False},
                                    "trace": [],
                                    "warnings": [str(exc) or "本次生成已取消"],
                                },
                            )
                        finally:
                            cdb.close()
                        emit("error", {"message": message, "detail": "本次生成已取消"})
                    except Exception:  # noqa: BLE001
                        emit("error", {"detail": "本次生成已取消"})
                except Exception as exc:  # noqa: BLE001 - surface a readable error
                    failure = classify_exception(exc)
                    audit.record(
                        event_type="chat_run",
                        component="agent",
                        action=str(payload.mode),
                        status="error",
                        duration_ms=int((time.perf_counter() - run_started) * 1000),
                        failure_class=failure.kind.value,
                        metadata={"error": str(exc)[:300]},
                        user_id=user["id"],
                        conversation_id=conversation_id,
                        request_id=getattr(request.state, "request_id", ""),
                    )
                    LOG.exception("chat failed (%s)", failure.kind.value)
                    try:
                        edb = connect(settings.db_path)
                        try:
                            message = repo.add_message(
                                edb,
                                conversation_id=conversation_id,
                                user_id=user["id"],
                                role="assistant",
                                content="抱歉，这次回答生成失败了。可以重试，或展开轨迹查看错误。",
                                meta={
                                    "mode": payload.mode,
                                    "engine": "local",
                                    "answer_mode": "error",
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "failure": failure.to_dict(),
                                    "trace": [],
                                    "warnings": [f"生成失败（{failure.kind.value}）"],
                                },
                            )
                        finally:
                            edb.close()
                        emit("error", {"message": message, "detail": "回答生成失败"})
                    except Exception:  # noqa: BLE001
                        emit("error", {"detail": "回答生成失败"})
                finally:
                    lease.release()
                    active_runs.finish(conversation_id, cancel_token)
                    emit("__end__", None)

            task = asyncio.create_task(asyncio.to_thread(work))

            async def timeout_guard() -> None:
                try:
                    await asyncio.sleep(gate.run_timeout)
                except asyncio.CancelledError:
                    return
                if task.done():
                    return
                cancel_token.cancel("本次生成超过运行时限，已取消")
                emit("status", {"title": "运行超时", "detail": "正在停止本次生成"})

            watchdog = asyncio.create_task(timeout_guard())
            yield _sse(
                "meta",
                {
                    "user_message": user_message,
                    "conversation_id": conversation_id,
                    "title": title or conversation["title"],
                },
            )
            disconnected = False
            while True:
                event, data = await queue.get()
                if event == "__end__":
                    break
                if event == "result":
                    message = data["message"]
                    for index, chunk in enumerate(_chunks(message["content"])):
                        if await request.is_disconnected():
                            disconnected = True
                            break
                        yield _sse("delta", {"text": chunk, "index": index})
                        await asyncio.sleep(0.012)
                    if not disconnected:
                        yield _sse("done", data)
                else:
                    yield _sse(event, data)
            watchdog.cancel()
            if not disconnected:
                try:
                    await asyncio.wait_for(task, timeout=1)
                except Exception:  # noqa: BLE001
                    pass
            else:
                # Let the worker save the assistant message even if the tab closed.
                task.add_done_callback(lambda _t: None)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
            background=BackgroundTask(lease.release),
        )

    # ------------------------------------------------------------ knowledge

    @app.get("/api/kb/overview")
    def kb_overview(
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        return resolve_kb(user, kb_id).overview()

    MAX_KB_UPLOAD = 10 * 1024 * 1024

    def can_manage_kb(user: dict[str, Any], kb_service: KnowledgeService) -> bool:
        role = str(user.get("role") or "member")
        if role == "admin":
            return True
        return role == "manager" and kb_service.info.department == str(user.get("department") or "")

    @app.get("/api/kb/documents")
    def kb_documents(
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        service = resolve_kb(user, kb_id)
        return {
            "kb": service.info.id,
            "name": service.info.name,
            "documents": service.documents(),
            "can_manage": can_manage_kb(user, service),
        }

    @app.post("/api/kb/documents")
    async def upload_kb_document(
        file: UploadFile = File(...),
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        service = resolve_kb(user, kb_id)
        if not can_manage_kb(user, service):
            raise HTTPException(status_code=403, detail="需要管理员或本部门 manager 权限")
        raw_name = Path(file.filename or "").name.strip()
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", raw_name)[:120]
        suffix = Path(safe_name).suffix.lower()
        supported = {str(item).lower() for item in SUPPORTED_SUFFIXES}
        if not safe_name or suffix not in supported:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型；允许 {', '.join(sorted(supported))}",
            )
        payload = await file.read(MAX_KB_UPLOAD + 1)
        if len(payload) > MAX_KB_UPLOAD:
            raise HTTPException(status_code=413, detail="文件超过 10MB")
        if not payload:
            raise HTTPException(status_code=400, detail="文件为空")
        service.knowledge_dir.mkdir(parents=True, exist_ok=True)
        target = (service.knowledge_dir / safe_name).resolve()
        try:
            target.relative_to(service.knowledge_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="非法文件名") from exc
        target.write_bytes(payload)
        report = service.ingest_file(target)
        if report.get("status") == "error":
            raise HTTPException(status_code=400, detail=report.get("detail") or "解析失败")
        return {
            "document": report,
            "documents": service.documents(),
            "overview": service.overview(),
        }

    @app.delete("/api/kb/documents/{doc_id}")
    def delete_kb_document(
        doc_id: str,
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        service = resolve_kb(user, kb_id)
        if not can_manage_kb(user, service):
            raise HTTPException(status_code=403, detail="需要管理员或本部门 manager 权限")
        removed = service.delete_document(doc_id)
        if not removed:
            raise HTTPException(status_code=404, detail="文档不存在")
        return {"removed": removed, "documents": service.documents(), "overview": service.overview()}

    @app.post("/api/kb/reindex")
    def reindex_kb(
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        service = resolve_kb(user, kb_id)
        if not can_manage_kb(user, service):
            raise HTTPException(status_code=403, detail="需要管理员或本部门 manager 权限")
        return {"result": service.reindex(), "overview": service.overview()}

    @app.get("/api/kb/graph")
    def kb_graph(
        query: str = Query(default="", max_length=200),
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        return resolve_kb(user, kb_id).graph_for_query(query)

    @app.get("/api/kb/search")
    def kb_search(
        q: str = Query(min_length=1, max_length=200),
        k: int = Query(default=6, ge=3, le=12),
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        return resolve_kb(user, kb_id).retrieve(q, k=k)

    @app.get("/api/kb/entities/{entity_id}")
    def kb_entity(
        entity_id: str,
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        detail = resolve_kb(user, kb_id).entity_detail(entity_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="实体不存在")
        return detail

    # --------------------------------------------------------------- static

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    docs_dir = settings.root / "docs"
    if docs_dir.exists():
        app.mount("/docs", StaticFiles(directory=docs_dir, html=True), name="docs")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(settings.static_dir / "favicon.svg", media_type="image/svg+xml")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(settings.static_dir / "index.html", media_type="text/html")

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or new_request_id()
        rid_token = request_id_var.set(rid)
        request.state.request_id = rid
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            await asyncio.to_thread(
                audit.record,
                event_type="api_request",
                component="http",
                action=f"{request.method} {request.url.path}"[:160],
                status="error",
                duration_ms=int((time.perf_counter() - started) * 1000),
                failure_class="unhandled",
                metadata={"path": request.url.path[:300]},
                user_id=getattr(request.state, "user_id", ""),
                request_id=rid,
            )
            request_id_var.reset(rid_token)
            raise
        duration = int((time.perf_counter() - started) * 1000)
        if request.url.path.startswith("/api/") and request.url.path not in {"/api/health"}:
            await asyncio.to_thread(
                audit.record,
                event_type="api_request",
                component="http",
                action=f"{request.method} {request.url.path}"[:160],
                status="error" if response.status_code >= 500 else "ok",
                duration_ms=duration,
                metadata={"status_code": response.status_code},
                user_id=getattr(request.state, "user_id", ""),
                request_id=rid,
            )
        response.headers["X-Request-ID"] = rid
        request_id_var.reset(rid_token)
        return response

    @app.middleware("http")
    async def no_stale_shell(request: Request, call_next):
        response = await call_next(request)
        if (
            request.url.path in {"/", "/index.html", "/docs/", "/docs/index.html"}
            or request.url.path.startswith("/static/")
            or request.url.path.startswith("/docs/")
        ):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        return response

    return app
