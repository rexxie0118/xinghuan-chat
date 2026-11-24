"""MCP clients and a small, explicit tool registry.

The registry is intentionally boring: a server must be configured in
``webchat/mcp.json`` (or ``WEBCHAT_MCP_CONFIG``) and explicitly allow the tools
that may be called. Tool output is truncated before it reaches an LLM or API
response.

Supported transports:
  * stdio: newline-delimited JSON-RPC 2.0 to a child process
  * http:  Streamable HTTP JSON-RPC (JSON or SSE response)

No external MCP SDK is required. Tool execution is separate from the local
Agent sandbox; each MCP server is an independently configured trust boundary.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

LOG = logging.getLogger("webchat.tools")
MAX_TOOL_RESULT_CHARS = 20_000
PROTOCOL_VERSION = "2024-11-05"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ToolError(RuntimeError):
    """A tool was unavailable, denied, timed out, or returned an error."""


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _truncate(text: str) -> str:
    text = str(text or "")
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + "\n… (工具输出已截断)"


def _jsonrpc_error(data: dict[str, Any]) -> ToolError:
    err = data.get("error") or {}
    message = err.get("message") or "MCP 返回错误"
    code = err.get("code")
    return ToolError(f"MCP error {code}: {message}" if code is not None else str(message))


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    server: str
    raw_name: str
    timeout: float = 10.0
    dangerous: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema or {"type": "object", "properties": {}},
            "server": self.server,
            "dangerous": self.dangerous,
        }


@dataclass
class ToolResult:
    ok: bool
    tool: str
    content: str = ""
    server: str = ""
    duration_ms: int = 0
    is_error: bool = False
    structured: Any = None
    url: str = ""

    def public(self) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "tool": self.tool,
            "server": self.server,
            "content": _truncate(self.content),
            "is_error": self.is_error,
            "duration_ms": self.duration_ms,
        }
        if self.structured is not None:
            out["structured"] = self.structured
        if self.url:
            out["url"] = self.url
        return out


class MCPClient:
    name = "mcp"

    def initialize(self) -> None:
        raise NotImplementedError

    def list_tools(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class StdioMCPClient(MCPClient):
    def __init__(self, server: str, config: dict[str, Any]) -> None:
        self.name = server
        self.config = _expand(config)
        self.timeout = float(self.config.get("timeout") or 10)
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stderr = ""
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self._next_id = 0

    # ------------------------------------------------------------ process
    def _command(self) -> list[str]:
        command = self.config.get("command") or ""
        args = [str(x) for x in (self.config.get("args") or [])]
        if isinstance(command, list):
            return [str(x) for x in command] + args
        if not command:
            raise ToolError(f"MCP stdio server {self.name!r} 没有 command")
        return [str(command)] + args

    def _start(self) -> None:
        if self._proc is not None:
            return
        env = {
            "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.getenv("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
        }
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})
        cwd = self.config.get("cwd") or None
        try:
            self._proc = subprocess.Popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                cwd=cwd,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"无法启动 MCP stdio server {self.name}: {type(exc).__name__}") from exc
        self._reader = threading.Thread(target=self._read_stdout, name=f"mcp-{self.name}", daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, name=f"mcp-{self.name}-err", daemon=True)
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            self._queue.put(None)
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    self._queue.put(data)
        finally:
            self._queue.put(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                self._stderr = (self._stderr + line)[-4000:]
        except Exception:  # noqa: BLE001
            pass

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._start()
        assert self._proc is not None and self._proc.stdin is not None
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"MCP stdin 写入失败：{type(exc).__name__}") from exc
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise ToolError(f"MCP {self.name} 响应超时（{self.timeout:g}s）")
            try:
                data = self._queue.get(timeout=remaining)
            except queue.Empty:
                self.close()
                raise ToolError(f"MCP {self.name} 响应超时（{self.timeout:g}s）")
            if data is None:
                detail = self._stderr.strip()
                self.close()
                raise ToolError(f"MCP {self.name} 进程已退出" + (f"：{detail[-300:]}" if detail else ""))
            if data.get("id") != request_id:
                continue
            if "error" in data:
                raise _jsonrpc_error(data)
            return data.get("result") or {}

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._start()
        assert self._proc is not None and self._proc.stdin is not None
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------------- API
    def initialize(self) -> None:
        with self._lock:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "xinghuan-chat", "version": "0.1.0"},
                },
            )
            self._notify("notifications/initialized")

    def list_tools(self) -> list[dict[str, Any]]:
        with self._lock:
            result = self._rpc("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return [t for t in tools or [] if isinstance(t, dict) and t.get("name")]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:  # noqa: BLE001
                pass


class HttpMCPClient(MCPClient):
    def __init__(self, server: str, config: dict[str, Any]) -> None:
        self.name = server
        self.config = _expand(config)
        self.timeout = float(self.config.get("timeout") or 10)
        url = str(self.config.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ToolError(f"MCP http server {server!r} 配置了非法 URL")
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **{str(k): str(v) for k, v in (self.config.get("headers") or {}).items()},
        }
        self.session_id = ""
        self._next_id = 0
        self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)

    def _parse(self, resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            raise ToolError(f"MCP {self.name} HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            last: dict[str, Any] | None = None
            for line in resp.iter_lines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        last = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
            if last is None:
                raise ToolError(f"MCP {self.name} 没有返回 SSE data")
            data = last
        else:
            try:
                resp.read()
                data = resp.json()
            except ValueError as exc:
                raise ToolError(f"MCP {self.name} 返回了非 JSON 内容") from exc
        if not isinstance(data, dict):
            raise ToolError(f"MCP {self.name} 返回格式错误")
        if "error" in data:
            raise _jsonrpc_error(data)
        return data.get("result") or {}

    def _post(self, method: str, params: dict[str, Any] | None = None, *, notify: bool = False) -> dict[str, Any]:
        self._next_id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            payload["id"] = self._next_id
        if params is not None:
            payload["params"] = params
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        try:
            with self._client.stream("POST", self.url, json=payload, headers=headers) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                if notify:
                    return {}
                return self._parse(resp)
        except ToolError:
            raise
        except httpx.HTTPError as exc:
            raise ToolError(f"MCP {self.name} 网络错误：{type(exc).__name__}") from exc

    def initialize(self) -> None:
        self._post(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "xinghuan-chat", "version": "0.1.0"},
            },
        )
        try:
            self._post("notifications/initialized", notify=True)
        except ToolError:
            pass

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._post("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        return [t for t in tools or [] if isinstance(t, dict) and t.get("name")]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._post("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        self._client.close()


def _normalize_content(result: dict[str, Any]) -> tuple[str, Any]:
    content = result.get("content")
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("type") == "resource":
                resource = item.get("resource") or {}
                parts.append(str(resource.get("text") or resource.get("uri") or ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
        text = "\n".join(p for p in parts if p)
    elif isinstance(content, str):
        text = content
    elif structured is not None:
        text = json.dumps(structured, ensure_ascii=False)
    else:
        text = json.dumps(result, ensure_ascii=False) if result else ""
    return text, structured



class MCPClientPool:
    """A small, failover-capable pool for one MCP server.

    Each shard is an independent MCP session/process. Calls are round-robin;
    if one shard fails, the next shard gets the call. Tool discovery uses the
    first healthy shard (replicas are expected to expose the same tools).
    """

    def __init__(self, server: str, clients: list[MCPClient]) -> None:
        self.server = server
        self.clients = clients
        self._index = 0
        self._lock = threading.RLock()
        self.calls = 0
        self.failures = 0

    def __len__(self) -> int:
        return len(self.clients)

    def list_tools(self) -> list[dict[str, Any]]:
        last: Exception | None = None
        for client in self.clients:
            try:
                return client.list_tools()
            except Exception as exc:  # noqa: BLE001
                last = exc
        raise ToolError(f"MCP {self.server} 所有分片都不可用：{last}")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            start = self._index
            self._index = (self._index + 1) % max(1, len(self.clients))
            self.calls += 1
        last: Exception | None = None
        for offset in range(len(self.clients)):
            client = self.clients[(start + offset) % len(self.clients)]
            try:
                return client.call_tool(name, arguments)
            except Exception as exc:  # noqa: BLE001 - fail over to next shard
                last = exc
                self.failures += 1
                LOG.warning(
                    "MCP shard failed: server=%s client=%s error=%s",
                    self.server,
                    getattr(client, "name", "?"),
                    type(exc).__name__,
                )
        raise ToolError(f"MCP {self.server} 所有分片都失败：{last}")

    def public(self) -> dict[str, Any]:
        return {
            "shards": len(self.clients),
            "calls": self.calls,
            "failures": self.failures,
        }

    def close(self) -> None:
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self.clients = []

class ToolRegistry:
    """Discovers MCP tools and enforces the configured allowlist."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.servers = self.config.get("servers") or {}
        self._clients: dict[str, MCPClientPool] = {}
        self._specs: dict[str, ToolSpec] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, Any] = {}
        self._lock = threading.RLock()

    def discover(self) -> "ToolRegistry":
        with self._lock:
            for server_name, raw_config in self.servers.items():
                if not isinstance(raw_config, dict):
                    continue
                status: dict[str, Any] = {
                    "name": server_name,
                    "enabled": bool(raw_config.get("enabled", True)),
                    "connected": False,
                    "error": "",
                    "tools": 0,
                    "transport": str(raw_config.get("transport") or "stdio"),
                }
                self._status[server_name] = status
                if not status["enabled"]:
                    continue
                clients: list[MCPClient] = []
                try:
                    transport = status["transport"].lower()
                    shards = int(raw_config.get("shards") or raw_config.get("pool_size") or 1)
                    shards = max(1, min(shards, 8))
                    urls = raw_config.get("urls") or []
                    tools: list[dict[str, Any]] = []
                    for shard in range(shards):
                        shard_config = dict(raw_config)
                        if urls:
                            shard_config["url"] = urls[shard % len(urls)]
                        if transport == "stdio":
                            client = StdioMCPClient(server_name, shard_config)
                        elif transport in {"http", "https", "streamable-http"}:
                            client = HttpMCPClient(server_name, shard_config)
                        else:
                            raise ToolError(f"不支持的 MCP transport：{transport}")
                        try:
                            client.initialize()
                            if not tools:
                                tools = client.list_tools()
                            clients.append(client)
                        except Exception as exc:  # noqa: BLE001
                            try:
                                client.close()
                            except Exception:  # noqa: BLE001
                                pass
                            LOG.warning(
                                "MCP shard unavailable: server=%s shard=%s error=%s",
                                server_name,
                                shard,
                                exc,
                            )
                    if not clients:
                        raise ToolError("所有 MCP 分片都不可用")
                    raw_allowed = raw_config.get("allow_tools") or ["*"]
                    if isinstance(raw_allowed, str):
                        raw_allowed = [raw_allowed]
                    allowed = {str(x) for x in raw_allowed}
                    for tool in tools:
                        raw_name = str(tool.get("name") or "")
                        if not raw_name:
                            continue
                        if "*" not in allowed and raw_name not in allowed:
                            continue
                        name = f"{server_name}.{raw_name}"
                        self._specs[name] = ToolSpec(
                            name=name,
                            description=str(tool.get("description") or ""),
                            input_schema=tool.get("inputSchema") or {},
                            server=server_name,
                            raw_name=raw_name,
                            timeout=float(raw_config.get("timeout") or 10),
                            dangerous=bool(raw_config.get("dangerous")),
                        )
                    pool = MCPClientPool(server_name, clients)
                    self._clients[server_name] = pool
                    status["connected"] = True
                    status["tools"] = len(tools)
                    status["shards"] = len(clients)
                except Exception as exc:  # noqa: BLE001
                    status["error"] = f"{type(exc).__name__}: {exc}"
                    for client in clients:
                        try:
                            client.close()
                        except Exception:  # noqa: BLE001
                            pass
                    LOG.warning("MCP server %s unavailable: %s", server_name, exc)
            return self

    def register_local(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None,
        handler: Any,
        timeout: float = 10.0,
        dangerous: bool = False,
    ) -> None:
        """Register an in-process, trusted plugin tool."""
        if not name or "." not in name:
            raise ToolError("插件工具名必须是 <plugin>.<tool>")
        if not callable(handler):
            raise ToolError("插件工具 handler 必须可调用")
        with self._lock:
            self._specs[name] = ToolSpec(
                name=name,
                description=description,
                input_schema=input_schema or {"type": "object", "properties": {}},
                server="plugin",
                raw_name=name,
                timeout=float(timeout),
                dangerous=dangerous,
            )
            self._handlers[name] = handler

    def list_tools(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda s: s.name)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolError(f"工具不存在或未授权：{name}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolError("工具参数必须是 JSON object")
        started = time.perf_counter()
        if name in self._handlers:
            try:
                raw = self._handlers[name](arguments)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("plugin tool failed: tool=%s error=%s", name, type(exc).__name__)
                raise ToolError(f"插件工具调用失败：{type(exc).__name__}: {exc}") from exc
            duration = int((time.perf_counter() - started) * 1000)
            if isinstance(raw, ToolResult):
                raw.duration_ms = duration
                return raw
            if not isinstance(raw, dict):
                raw = {"content": str(raw)}
            text, structured = _normalize_content(raw)
            is_error = bool(raw.get("isError"))
            return ToolResult(
                ok=not is_error,
                tool=name,
                content=_truncate(text),
                server="plugin",
                duration_ms=duration,
                is_error=is_error,
                structured=structured,
                url=str(raw.get("url") or ""),
            )
        client = self._clients.get(spec.server)
        if client is None:
            raise ToolError(f"MCP server 未连接：{spec.server}")
        try:
            raw = client.call_tool(spec.raw_name, arguments)
        except ToolError as exc:
            LOG.warning("tool call failed: tool=%s server=%s error=%s", name, spec.server, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            LOG.warning("tool call failed: tool=%s server=%s error=%s", name, spec.server, type(exc).__name__)
            raise ToolError(f"工具调用失败：{type(exc).__name__}: {exc}") from exc
        duration = int((time.perf_counter() - started) * 1000)
        if not isinstance(raw, dict):
            raw = {"content": str(raw)}
        text, structured = _normalize_content(raw)
        is_error = bool(raw.get("isError"))
        LOG.info(
            "tool call: tool=%s server=%s ok=%s duration_ms=%d chars=%d",
            name,
            spec.server,
            not is_error,
            duration,
            len(text),
        )
        return ToolResult(
            ok=not is_error,
            tool=name,
            content=_truncate(text),
            server=spec.server,
            duration_ms=duration,
            is_error=is_error,
            structured=structured,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": any(status.get("connected") for status in self._status.values()),
            "servers": [
                {
                    **status,
                    **(
                        self._clients[status["name"]].public()
                        if status["name"] in self._clients
                        else {}
                    ),
                }
                for status in sorted(self._status.values(), key=lambda s: s["name"])
            ],
            "tools": [spec.public() for spec in self.list_tools()],
        }

    def close(self) -> None:
        for client in list(self._clients.values()):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()


def load_mcp_config(path: Path | str | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        LOG.warning("Invalid MCP config %s: %s", p, exc)
        return {}
    return data if isinstance(data, dict) else {}


def build_tool_registry(settings: Any) -> ToolRegistry:
    if not getattr(settings, "tools_enabled", True):
        return ToolRegistry({})
    path = getattr(settings, "mcp_config", None) or (Path(settings.root) / "mcp.json")
    config = load_mcp_config(path)
    registry = ToolRegistry(config)
    if config:
        registry.discover()
    return registry
