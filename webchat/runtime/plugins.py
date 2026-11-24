"""Trusted in-process plugin host and manifest loader.

A plugin is code running in the WebChat process with the same OS permissions.
Only explicitly trusted manifests are loaded. Tenant code must not be loaded
here; use MCP or a sandboxed Agent for untrusted extensions.
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .hooks import HookBus, HookContext

LOG = logging.getLogger("webchat.runtime.plugins")
MANIFEST_VERSION = 1
KNOWN_EVENTS = {
    "agent_start",
    "agent_end",
    "agent_error",
    "agent_cancelled",
    "retrieval_start",
    "retrieval_end",
    "answer_start",
    "answer_end",
    "tool_call",
    "tool_result",
}
KNOWN_PERMISSIONS = {"hooks", "tools", "secrets", "network", "dangerous_tools", "capabilities"}
TRUST_LEVELS = {"system", "trusted", "untrusted"}
INTERNAL_PREFIX = "webchat.plugins."


@dataclass
class PluginManifest:
    name: str
    version: str
    entrypoint: str
    description: str = ""
    enabled: bool = True
    trusted: bool = False
    permissions: set[str] = field(default_factory=set)
    hooks: set[str] = field(default_factory=set)
    trust: str = "untrusted"
    tool_allowlist: set[str] = field(default_factory=set)
    secrets: set[str] = field(default_factory=set)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PluginManifest":
        name = str(data.get("name") or "").strip()
        version = str(data.get("version") or "").strip()
        entrypoint = str(data.get("entrypoint") or "").strip()
        if not name or not version or not entrypoint:
            raise ValueError("manifest 需要 name / version / entrypoint")
        if ":" not in entrypoint:
            raise ValueError("entrypoint 必须是 module:attr")
        permissions = {str(p) for p in (data.get("permissions") or [])}
        hooks = {str(h) for h in (data.get("hooks") or [])}
        unknown = permissions - KNOWN_PERMISSIONS
        if unknown:
            raise ValueError(f"未知权限：{', '.join(sorted(unknown))}")
        unknown_hooks = hooks - KNOWN_EVENTS
        if unknown_hooks:
            raise ValueError(f"未知 hook：{', '.join(sorted(unknown_hooks))}")
        trusted = bool(data.get("trusted", False))
        trust = str(data.get("trust") or "").strip().lower()
        if trust and trust not in TRUST_LEVELS:
            raise ValueError(f"未知 trust：{trust}")
        if not trust:
            trust = "trusted" if trusted else "untrusted"
        if trusted and trust == "untrusted":
            raise ValueError("trusted=true 与 trust=untrusted 冲突")
        tool_allowlist = {str(item) for item in (data.get("tool_allowlist") or [])}
        secrets = {str(item) for item in (data.get("secrets") or [])}
        if secrets and "secrets" not in permissions:
            raise ValueError("声明 secrets 时必须包含 secrets 权限")
        return cls(
            name=name,
            version=version,
            entrypoint=entrypoint,
            description=str(data.get("description") or ""),
            enabled=bool(data.get("enabled", True)),
            trusted=trust in {"trusted", "system"},
            permissions=permissions,
            hooks=hooks,
            trust=trust,
            tool_allowlist=tool_allowlist,
            secrets=secrets,
        )

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "trusted": self.trusted,
            "trust": self.trust,
            "permissions": sorted(self.permissions),
            "hooks": sorted(self.hooks),
            "tool_allowlist": sorted(self.tool_allowlist),
            "secrets": sorted(self.secrets),
        }


@dataclass
class Capability:
    kind: str
    name: str
    handler: Any
    plugin: str
    priority: int = 100

    def public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "plugin": self.plugin,
            "priority": self.priority,
        }


@dataclass
class PluginStatus:
    manifest: PluginManifest
    status: str = "pending"
    error: str = ""

    def public(self) -> dict[str, Any]:
        return {**self.manifest.public(), "status": self.status, "error": self.error}


class PluginContext:
    def __init__(self, host: "PluginHost", manifest: PluginManifest) -> None:
        self.host = host
        self.manifest = manifest
        self.settings = host.settings
        self.kernel = host.kernel
        self.logger = logging.getLogger(f"webchat.plugin.{manifest.name}")
        self.data: dict[str, Any] = {}
        self.services = host.services

    def register_capability(self, kind: str, name: str, handler, *, priority: int = 100) -> None:
        if "capabilities" not in self.manifest.permissions:
            raise PermissionError("插件没有 capabilities 权限")
        if not callable(handler):
            raise TypeError("capability handler 必须可调用")
        self.host.register_capability(
            kind=str(kind),
            name=str(name),
            handler=handler,
            plugin=self.manifest.name,
            priority=int(priority),
        )

    def on(self, event: str, handler) -> None:
        if event not in KNOWN_EVENTS:
            raise ValueError(f"未知 hook：{event}")
        if "hooks" not in self.manifest.permissions:
            raise PermissionError("插件没有 hooks 权限")
        if self.manifest.hooks and event not in self.manifest.hooks:
            raise PermissionError(f"插件 manifest 未允许 hook：{event}")
        self.kernel.hooks.on(event, handler)

    def secret(self, name: str) -> str:
        if "secrets" not in self.manifest.permissions:
            raise PermissionError("插件没有 secrets 权限")
        if name not in self.manifest.secrets:
            raise PermissionError(f"插件 manifest 未允许 secret：{name}")
        return str(__import__("os").getenv(name, ""))

    def register_tool(
        self,
        name: str,
        *,
        description: str,
        input_schema: dict[str, Any] | None,
        handler,
        timeout: float = 10.0,
        dangerous: bool = False,
    ) -> None:
        if "tools" not in self.manifest.permissions:
            raise PermissionError("插件没有 tools 权限")
        if self.manifest.tool_allowlist and name not in self.manifest.tool_allowlist:
            raise PermissionError(f"插件 manifest 未允许工具：{name}")
        if dangerous and "dangerous_tools" not in self.manifest.permissions:
            raise PermissionError("注册 dangerous 工具需要 dangerous_tools 权限")
        self.host.register_tool(
            self.manifest.name,
            name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            timeout=timeout,
            dangerous=dangerous,
        )


class PluginHost:
    def __init__(
        self,
        settings: Any,
        kernel: Any,
        tools: Any,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings
        self.kernel = kernel
        self.tools = tools
        self.services = services or {}
        self.statuses: list[PluginStatus] = []
        self.capabilities: dict[str, list[Capability]] = {}

    def load(self) -> "PluginHost":
        if not getattr(self.settings, "plugins_enabled", True):
            return self
        path = getattr(self.settings, "plugins_config", None)
        if not path:
            path = Path(self.settings.root) / "plugins.json"
        path = Path(path)
        if not path.exists():
            return self
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            LOG.warning("plugin config invalid: %s", exc)
            return self
        entries = payload.get("plugins") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            LOG.warning("plugin config must contain a plugins list")
            return self
        for raw in entries:
            if not isinstance(raw, dict):
                continue
            try:
                manifest = PluginManifest.from_dict(raw)
            except ValueError as exc:
                LOG.warning("plugin manifest invalid: %s", exc)
                continue
            status = PluginStatus(manifest)
            self.statuses.append(status)
            if not manifest.enabled:
                status.status = "disabled"
                continue
            try:
                self._load_one(manifest)
                status.status = "loaded"
            except Exception as exc:  # noqa: BLE001
                status.status = "error"
                status.error = f"{type(exc).__name__}: {exc}"
                LOG.warning("plugin %s failed: %s", manifest.name, exc)
        return self

    def _load_one(self, manifest: PluginManifest) -> None:
        if manifest.trust not in {"trusted", "system"}:
            raise PermissionError("插件不是 trusted/system，拒绝加载")
        if not getattr(self.settings, "plugins_allow_external", False):
            if not manifest.entrypoint.startswith(INTERNAL_PREFIX):
                raise PermissionError("默认只允许 webchat.plugins.* 内部插件")
        module_name, attr = manifest.entrypoint.split(":", 1)
        module = importlib.import_module(module_name)
        target = module
        for part in attr.split("."):
            target = getattr(target, part)
        plugin = target() if isinstance(target, type) else target
        if not hasattr(plugin, "setup"):
            raise TypeError("插件必须提供 setup(ctx)")
        plugin.setup(PluginContext(self, manifest))

    def register_tool(
        self,
        plugin_name: str,
        tool_name: str,
        *,
        description: str,
        input_schema: dict[str, Any] | None,
        handler,
        timeout: float,
        dangerous: bool,
    ) -> None:
        full = f"plugin.{plugin_name}.{tool_name}"
        self.tools.register_local(
            name=full,
            description=description,
            input_schema=input_schema,
            handler=handler,
            timeout=timeout,
            dangerous=dangerous,
        )

    def load_builtins(self) -> "PluginHost":
        builtins = [
            ("rag", "webchat.plugins.rag:plugin"),
            ("imported_agent", "webchat.plugins.imported_agent:plugin"),
            ("mcp", "webchat.plugins.mcp:plugin"),
        ]
        if any(status.manifest.name == name for name, _ in builtins for status in self.statuses):
            return self
        for name, entrypoint in builtins:
            manifest = PluginManifest(
                name=name,
                version="1.0.0",
                entrypoint=entrypoint,
                description=f"builtin {name} adapter",
                trust="system",
                trusted=True,
                permissions={"capabilities"},
            )
            status = PluginStatus(manifest)
            self.statuses.append(status)
            try:
                self._load_one(manifest)
                status.status = "loaded"
            except Exception as exc:  # noqa: BLE001
                status.status = "error"
                status.error = f"{type(exc).__name__}: {exc}"
                LOG.warning("builtin plugin %s failed: %s", name, exc)
        return self

    def register_capability(
        self,
        *,
        kind: str,
        name: str,
        handler,
        plugin: str,
        priority: int = 100,
    ) -> None:
        self.capabilities.setdefault(kind, []).append(
            Capability(kind=kind, name=name, handler=handler, plugin=plugin, priority=priority)
        )
        self.capabilities[kind].sort(key=lambda item: item.priority)

    def get_capability(self, kind: str, name: str | None = None):
        items = self.capabilities.get(kind) or []
        for item in items:
            if name is None or item.name == name:
                return item
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.statuses),
            "plugins": [s.public() for s in self.statuses],
            "capabilities": [
                item.public() for items in self.capabilities.values() for item in items
            ],
        }


def load_plugin_host(
    settings: Any,
    kernel: Any,
    tools: Any,
    services: dict[str, Any] | None = None,
) -> PluginHost:
    return PluginHost(settings, kernel, tools, services=services).load()
