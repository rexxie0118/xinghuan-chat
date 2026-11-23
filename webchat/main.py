"""FastAPI application for 星环 Chat."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sqlite3
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal
from urllib.parse import quote, urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import repository as repo
from .agent import AgentRunner
from .artifacts import LocalArtifactStore, owns_run, record_run
from .agent_defs import (
    agent_detail,
    build_agent,
    delete_agent,
    get_agent,
    list_agents,
    save_agent,
)
from .github_importer import GitHubImportError, import_from_github
from .config import Settings
from .db import connect, init_db
from .knowledge import KnowledgeRegistry, KnowledgeService
from .sandbox import build_sandbox_runner
from .sandbox.apple_runtime import AppleContainerRuntime
from .tools import ToolError, build_tool_registry
from .runtime import AgentKernel
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
            )
            LOG.info("Seeded demo user %s (%s)", account["username"], account["department"])
        elif row["department"] != account["department"]:
            # Upgrade databases seeded before department support.
            repo.set_user_department(conn, row["id"], account["department"])


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

    registry = KnowledgeRegistry(settings)
    sandbox = build_sandbox_runner(settings)
    container_runtime = AppleContainerRuntime(settings)
    runner = AgentRunner(settings, sandbox)
    kernel = AgentKernel(settings, runner)
    tools = build_tool_registry(settings)

    conn = connect(settings.db_path)
    try:
        if settings.seed_demo_users:
            _seed_demo_users(conn)
        repo.purge_expired_sessions(conn)
    finally:
        conn.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        LOG.info("星环 Chat ready at %s", settings.db_path)
        try:
            yield
        finally:
            tools.close()

    app = FastAPI(
        title=APP_NAME,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.kb = registry.get(registry.infos[0].id)
    app.state.sandbox = sandbox
    app.state.container_runtime = container_runtime
    app.state.runner = runner
    app.state.kernel = kernel
    app.state.tools = tools

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
            "departments": registry.departments,
            "default_department": registry.default_department,
            "knowledge_bases": registry.catalog_public(),
            "tools": tools.summary(),
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
            except sqlite3.IntegrityError as exc:
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
            return {"agents": list_agents(db, user["id"])}
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
        path = LocalArtifactStore(settings.data_dir).path_for(run_id, name)
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
            agent = get_agent(db, agent_id, user["id"])
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
            agent = get_agent(db, agent_id, user["id"])
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
            data = agent_detail(db, agent_id, user["id"])
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
                imported_agent = get_agent(db, payload.agent_id, user["id"])
                if imported_agent is None:
                    raise HTTPException(status_code=404, detail="Agent 不存在或不属于你")
            history = repo.conversation_context(
                db, conversation_id=conversation_id, user_id=user["id"], limit=8
            )
            user_message = repo.add_message(
                db,
                conversation_id=conversation_id,
                user_id=user["id"],
                role="user",
                content=content,
                meta={"mode": payload.mode, "agent_id": payload.agent_id},
            )
            title = repo.auto_title_if_needed(
                db,
                conversation_id=conversation_id,
                user_id=user["id"],
                first_user_text=content,
            )
        finally:
            db.close()

        async def event_stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def emit(event: str, data: Any) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, (event, data))

            def work() -> None:
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
                    )
                    wdb = connect(settings.db_path)
                    try:
                        meta = result.message_meta()
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
                    finally:
                        wdb.close()
                    emit(
                        "result",
                        {
                            "message": message,
                            "conversation": fresh or conversation,
                            "user_message": user_message,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 - surface a readable error
                    LOG.exception("chat failed")
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
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "trace": [],
                                    "warnings": ["生成失败"],
                                },
                            )
                        finally:
                            edb.close()
                        emit("error", {"message": message, "detail": "回答生成失败"})
                    except Exception:  # noqa: BLE001
                        emit("error", {"detail": "回答生成失败"})
                finally:
                    emit("__end__", None)

            task = asyncio.create_task(asyncio.to_thread(work))
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
        )

    # ------------------------------------------------------------ knowledge

    @app.get("/api/kb/overview")
    def kb_overview(
        kb_id: str = Query(default="", alias="kb", max_length=40),
        user: dict[str, Any] = User,
    ) -> dict[str, Any]:
        return resolve_kb(user, kb_id).overview()

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

    return app
