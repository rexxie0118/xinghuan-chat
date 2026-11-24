"""Declarative HTTP connectors.

Connectors are data, not generated code: the LLM proposes a spec, the host
validates hosts/params/auth, a test request exercises it, and only then does it
become a ToolRegistry tool. This is the safe middle ground between a fixed tool
catalog and arbitrary generated Python.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .db import connect
from .util import iso, new_id

MAX_RESPONSE_BYTES = 256_000
NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{1,40}$")


class ConnectorError(RuntimeError):
    pass


@dataclass
class ConnectorSpec:
    name: str
    description: str
    base_url: str
    method: str
    path: str
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    fixed: dict[str, Any] = field(default_factory=dict)
    response_path: str = ""
    auth: dict[str, str] = field(default_factory=dict)
    allow_hosts: list[str] = field(default_factory=list)

    @property
    def tool_name(self) -> str:
        return f"connector.{self.name}"

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "base_url": self.base_url,
            "method": self.method,
            "path": self.path,
            "params": self.params,
            "fixed": self.fixed,
            "response_path": self.response_path,
            "auth": {k: v for k, v in self.auth.items() if k != "value"},
            "allow_hosts": self.allow_hosts,
            "tool_name": self.tool_name,
        }

    def tool_schema(self) -> dict[str, Any]:
        properties = {}
        required = []
        for name, config in self.params.items():
            properties[name] = {
                "type": config.get("type", "string"),
                "description": config.get("description", ""),
            }
            if config.get("required", True):
                required.append(name)
        return {"type": "object", "properties": properties, "required": required}


def parse_spec(raw: dict[str, Any]) -> ConnectorSpec:
    if not isinstance(raw, dict):
        raise ConnectorError("connector spec 必须是 JSON object")
    name = str(raw.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ConnectorError("name 只能使用字母、数字、下划线、短横线，2-41 位")
    description = str(raw.get("description") or "").strip()
    if not description:
        raise ConnectorError("description 不能为空")
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConnectorError("base_url 必须是 https://")
    method = str(raw.get("method") or "GET").upper()
    if method not in {"GET", "POST"}:
        raise ConnectorError("method 只支持 GET / POST")
    path = str(raw.get("path") or "").strip()
    if not path.startswith("/") or ".." in path:
        raise ConnectorError("path 必须以 / 开头且不能包含 ..")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise ConnectorError("params 必须是 object")
    for pname, config in params.items():
        if not NAME_RE.match(str(pname)):
            raise ConnectorError(f"非法参数名：{pname}")
        if not isinstance(config, dict):
            raise ConnectorError(f"参数 {pname} 必须是 object")
    fixed = raw.get("fixed") or {}
    if not isinstance(fixed, dict):
        raise ConnectorError("fixed 必须是 object")
    allow_hosts = [str(h).strip().lower() for h in (raw.get("allow_hosts") or [parsed.netloc])]
    if parsed.netloc.lower() not in allow_hosts:
        raise ConnectorError("allow_hosts 必须包含 base_url 的 host")
    auth = raw.get("auth") or {}
    if auth and not isinstance(auth, dict):
        raise ConnectorError("auth 必须是 object")
    if auth:
        if auth.get("type") not in {None, "header", "query"}:
            raise ConnectorError("auth.type 只支持 header / query")
        if not auth.get("env"):
            raise ConnectorError("auth.env 不能为空")
    return ConnectorSpec(
        name=name,
        description=description,
        base_url=base_url,
        method=method,
        path=path,
        params={str(k): dict(v) for k, v in params.items()},
        fixed=dict(fixed),
        response_path=str(raw.get("response_path") or ""),
        auth={str(k): str(v) for k, v in auth.items()},
        allow_hosts=allow_hosts,
    )


class ConnectorExecutor:
    def __init__(self, client_factory=None) -> None:
        self.client_factory = client_factory or (
            lambda timeout: httpx.Client(timeout=timeout, follow_redirects=False)
        )

    def execute(self, spec: ConnectorSpec, args: dict[str, Any], *, timeout: float = 12) -> dict[str, Any]:
        args = args or {}
        path = spec.path
        query: dict[str, Any] = dict(spec.fixed)
        body: dict[str, Any] = {}
        for name, config in spec.params.items():
            if name not in args:
                if config.get("required", True):
                    raise ConnectorError(f"缺少参数：{name}")
                continue
            value = args[name]
            where = config.get("in", "query")
            key = config.get("key", name)
            if where == "path":
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            elif where == "body":
                body[key] = value
            else:
                query[key] = value
        url = spec.base_url + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in spec.allow_hosts:
            raise ConnectorError(f"目标 host 不在 allowlist：{parsed.hostname}")
        headers: dict[str, str] = {}
        auth = spec.auth or {}
        if auth.get("env"):
            secret = __import__("os").getenv(str(auth["env"]), "")
            if not secret:
                raise ConnectorError(f"缺少环境变量 {auth['env']}")
            if auth.get("type") == "query":
                query[str(auth.get("key") or "api_key")] = secret
            else:
                headers[str(auth.get("key") or "Authorization")] = (
                    str(auth.get("prefix") or "") + secret
                )
        client = self.client_factory(timeout)
        try:
            if spec.method == "POST":
                resp = client.post(url, params=query, json=body, headers=headers)
            else:
                resp = client.get(url, params=query, headers=headers)
            if resp.status_code >= 400:
                raise ConnectorError(f"HTTP {resp.status_code}")
            content = resp.content
            if len(content) > MAX_RESPONSE_BYTES:
                raise ConnectorError("响应超过 256KB 限制")
            try:
                data = resp.json()
            except ValueError as exc:
                raise ConnectorError("响应不是 JSON") from exc
            if spec.response_path:
                for part in spec.response_path.split("."):
                    if isinstance(data, dict):
                        data = data.get(part)
                    elif isinstance(data, list) and part.isdigit():
                        data = data[int(part)]
                    else:
                        data = None
                        break
            text = json.dumps(data, ensure_ascii=False)
            return {
                "content": text[:8000],
                "structured": data,
                "url": str(resp.url),
            }
        finally:
            client.close()


class ConnectorStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)

    def _conn(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def create(self, user_id: str, spec: ConnectorSpec, *, source: str = "manual") -> dict[str, Any]:
        now = iso()
        conn = self._conn()
        try:
            with conn:
                existing = conn.execute(
                    "SELECT id, user_id FROM connectors WHERE name=?", (spec.name,)
                ).fetchone()
                if existing is not None and existing["user_id"] != user_id:
                    raise ConnectorError("连接器名称已被其他用户占用")
                if existing is None:
                    connector_id = new_id("conn")
                    conn.execute(
                        """
                        INSERT INTO connectors(id, user_id, name, description, spec_json,
                            status, source, created_at, updated_at, approved_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            connector_id,
                            user_id,
                            spec.name,
                            spec.description,
                            json.dumps(spec.public(), ensure_ascii=False),
                            "draft",
                            source,
                            now,
                            now,
                            "",
                        ),
                    )
                else:
                    connector_id = existing["id"]
                    conn.execute(
                        "UPDATE connectors SET description=?, spec_json=?, status='draft',"
                        " source=?, updated_at=?, approved_at='' WHERE id=?",
                        (
                            spec.description,
                            json.dumps(spec.public(), ensure_ascii=False),
                            source,
                            now,
                            connector_id,
                        ),
                    )
            return self.get(connector_id) or {}
        finally:
            conn.close()

    def get(self, connector_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            row = conn.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
            return self._public(row) if row else None
        finally:
            conn.close()

    def list(self, user_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM connectors WHERE user_id=? ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM connectors ORDER BY updated_at DESC").fetchall()
            return [self._public(row) for row in rows]
        finally:
            conn.close()

    def approve(self, connector_id: str, user_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE connectors SET status='approved', approved_at=?, updated_at=?"
                    " WHERE id=? AND user_id=?",
                    (iso(), iso(), connector_id, user_id),
                )
            return self.get(connector_id)
        finally:
            conn.close()

    def reject(self, connector_id: str, user_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            with conn:
                conn.execute(
                    "UPDATE connectors SET status='rejected', updated_at=?"
                    " WHERE id=? AND user_id=?",
                    (iso(), connector_id, user_id),
                )
            return self.get(connector_id)
        finally:
            conn.close()

    @staticmethod
    def _public(row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["spec"] = json.loads(item.pop("spec_json") or "{}")
        except ValueError:
            item["spec"] = {}
        return item


class ConnectorService:
    def __init__(self, settings: Any, tools: Any) -> None:
        self.settings = settings
        self.tools = tools
        self.store = ConnectorStore(settings.db_path)
        self.executor = ConnectorExecutor()

    def load_approved(self) -> int:
        count = 0
        for item in self.store.list():
            if item.get("status") != "approved":
                continue
            try:
                self.register(parse_spec(item["spec"]))
                count += 1
            except Exception:  # noqa: BLE001
                continue
        return count

    def register(self, spec: ConnectorSpec) -> None:
        def handler(args: dict[str, Any]):
            return self.executor.execute(spec, args)

        self.tools.register_local(
            name=spec.tool_name,
            description=spec.description,
            input_schema=spec.tool_schema(),
            handler=handler,
            timeout=20,
            dangerous=False,
        )


KNOWN_CONNECTORS: list[dict[str, Any]] = [
    {
        "name": "weather_geocode",
        "description": "把城市名解析为经纬度（Open-Meteo Geocoding）",
        "base_url": "https://geocoding-api.open-meteo.com/v1",
        "method": "GET",
        "path": "/search",
        "params": {"city": {"type": "string", "description": "城市名"}},
        "fixed": {"count": 1, "language": "zh", "format": "json"},
        "response_path": "results",
        "allow_hosts": ["geocoding-api.open-meteo.com"],
    },
    {
        "name": "weather_current",
        "description": "按经纬度查询当前天气（Open-Meteo）",
        "base_url": "https://api.open-meteo.com/v1",
        "method": "GET",
        "path": "/forecast",
        "params": {
            "latitude": {"type": "number", "description": "纬度"},
            "longitude": {"type": "number", "description": "经度"},
        },
        "fixed": {
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "timezone": "auto",
        },
        "response_path": "current",
        "allow_hosts": ["api.open-meteo.com"],
    },
]


def known_for(query: str) -> list[dict[str, Any]]:
    text = (query or "").lower()
    if any(word in text for word in ("天气", "weather", "气温", "温度")):
        return [dict(item) for item in KNOWN_CONNECTORS]
    return []


PROPOSE_SYSTEM = """你是星环 Chat 的连接器生成器。输出一个 JSON object，不要 Markdown。
只能生成只读的 HTTPS API 连接器，不允许写操作、任意代码、内部 IP。
字段：
name, description, base_url (https), method (GET/POST), path, params (object),
fixed (object), response_path (dot path), auth (optional {type,env,key,prefix}),
allow_hosts (array, 必须包含 base_url host)。
params 的 value 支持 type/description/in(query|path|body)/key/required。
不确定的 API 不要编造具体 endpoint；宁可返回 {"error":"..."}。
"""


def propose_with_llm(provider, query: str) -> dict[str, Any]:
    text = provider.complete(PROPOSE_SYSTEM, query, json_mode=True, max_tokens=1200)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConnectorError("模型没有返回合法 JSON") from exc
    if "error" in data:
        raise ConnectorError(str(data["error"]))
    return data
