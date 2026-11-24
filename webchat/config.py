"""Runtime settings for the 星环 Chat demo.

Everything is overridable from the environment so the app can run from a
checkout, a container, or a test sandbox without touching the code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR          # webchat package root
WORKSPACE = ROOT.parent # repository root that contains the kb engine


def _load_dotenv(path: Path) -> None:
    """Load webchat/.env without overriding variables already exported."""
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    db_path: Path
    kb_path: Path
    kb_config_path: Path
    knowledge_dir: Path
    static_dir: Path
    agent_url: str | None
    agent_token: str | None
    agent_timeout: float
    session_days: int
    secure_cookie: bool
    kb_provider: str | None
    seed_demo_users: bool
    allow_script_exec: bool = True
    sandbox: str = "local"
    mcp_config: Path | None = None
    tools_enabled: bool = True
    apple_container_bin: str = "container"
    apple_container_image: str = "python:3.12-slim"
    apple_container_cpus: int = 2
    apple_container_memory: str = "2G"
    apple_container_network: str = "default"
    apple_pool_enabled: bool = True
    apple_pool_min: int = 1
    apple_pool_max: int = 4
    apple_pool_per_user: int = 2
    runtime_auto_ensure: bool = True
    runtime_idle_enabled: bool = True
    runtime_idle_stop_seconds: int = 1800
    runtime_idle_delete_seconds: int = 604800
    runtime_idle_purge_seconds: int = 2592000
    runtime_reap_interval: int = 60
    plugins_enabled: bool = True
    plugins_config: Path | None = None
    plugins_allow_external: bool = False
    tool_loop_enabled: bool = True
    tool_max_steps: int = 4
    context_max_tokens: int = 12000
    context_reserve_tokens: int = 1200
    max_concurrent_runs: int = 4
    max_user_runs: int = 1
    max_queue: int = 32
    queue_timeout: float = 30.0
    run_timeout: float = 180.0
    queue_url: str = ""
    queue_name: str = "webchat:jobs"
    object_store: str = "local"
    s3_bucket: str = ""
    s3_prefix: str = "artifacts"
    s3_endpoint: str = ""
    s3_region: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(ROOT / ".env")
        data_dir = Path(os.getenv("WEBCHAT_DATA_DIR", ROOT / "data")).expanduser()
        kb_path = Path(os.getenv("WEBCHAT_KB_DB", data_dir / "kb.sqlite3")).expanduser()
        kb_config = Path(
            os.getenv("WEBCHAT_KB_CONFIG", ROOT / "kb.config.json")
        ).expanduser()
        settings = cls(
            root=ROOT,
            data_dir=data_dir,
            db_path=Path(os.getenv("WEBCHAT_DB", data_dir / "webchat.sqlite3")).expanduser(),
            kb_path=kb_path,
            kb_config_path=kb_config,
            knowledge_dir=Path(
                os.getenv("WEBCHAT_KNOWLEDGE_DIR", ROOT / "knowledge")
            ).expanduser(),
            static_dir=ROOT / "static",
            agent_url=(os.getenv("WEBCHAT_AGENT_URL") or "").strip() or None,
            agent_token=(os.getenv("WEBCHAT_AGENT_TOKEN") or "").strip() or None,
            agent_timeout=float(os.getenv("WEBCHAT_AGENT_TIMEOUT", "25")),
            session_days=int(os.getenv("WEBCHAT_SESSION_DAYS", "7")),
            secure_cookie=_env_bool("WEBCHAT_SECURE_COOKIE", False),
            kb_provider=(os.getenv("WEBCHAT_KB_PROVIDER") or "").strip() or None,
            seed_demo_users=_env_bool("WEBCHAT_SEED_DEMO_USERS", True),
            allow_script_exec=_env_bool("WEBCHAT_ALLOW_SCRIPT_EXEC", True),
            sandbox=(os.getenv("WEBCHAT_SANDBOX") or "local").strip(),
            mcp_config=Path(
                os.getenv("WEBCHAT_MCP_CONFIG", ROOT / "mcp.json")
            ).expanduser(),
            tools_enabled=_env_bool("WEBCHAT_TOOLS_ENABLED", True),
            apple_container_bin=(os.getenv("APPLE_CONTAINER_BIN") or "container").strip(),
            apple_container_image=(
                os.getenv("APPLE_CONTAINER_IMAGE") or "python:3.12-slim"
            ).strip(),
            apple_container_cpus=int(os.getenv("APPLE_CONTAINER_CPUS", "2")),
            apple_container_memory=(os.getenv("APPLE_CONTAINER_MEMORY") or "2G").strip(),
            apple_container_network=(os.getenv("APPLE_CONTAINER_NETWORK") or "default").strip(),
            apple_pool_enabled=_env_bool("APPLE_POOL_ENABLED", True),
            apple_pool_min=int(os.getenv("APPLE_POOL_MIN", "1")),
            apple_pool_max=int(os.getenv("APPLE_POOL_MAX", "4")),
            apple_pool_per_user=int(os.getenv("APPLE_POOL_PER_USER", "2")),
            runtime_auto_ensure=_env_bool("WEBCHAT_RUNTIME_AUTO_ENSURE", True),
            runtime_idle_enabled=_env_bool("WEBCHAT_RUNTIME_IDLE_ENABLED", True),
            runtime_idle_stop_seconds=int(os.getenv("WEBCHAT_RUNTIME_IDLE_STOP_SECONDS", "1800")),
            runtime_idle_delete_seconds=int(os.getenv("WEBCHAT_RUNTIME_IDLE_DELETE_SECONDS", "604800")),
            runtime_idle_purge_seconds=int(os.getenv("WEBCHAT_RUNTIME_IDLE_PURGE_SECONDS", "2592000")),
            runtime_reap_interval=int(os.getenv("WEBCHAT_RUNTIME_REAP_INTERVAL", "60")),
            plugins_enabled=_env_bool("WEBCHAT_PLUGINS_ENABLED", True),
            plugins_config=Path(
                os.getenv("WEBCHAT_PLUGINS_CONFIG", ROOT / "plugins.json")
            ).expanduser(),
            plugins_allow_external=_env_bool("WEBCHAT_PLUGINS_ALLOW_EXTERNAL", False),
            tool_loop_enabled=_env_bool("WEBCHAT_TOOL_LOOP_ENABLED", True),
            tool_max_steps=int(os.getenv("WEBCHAT_TOOL_MAX_STEPS", "4")),
            context_max_tokens=int(os.getenv("WEBCHAT_CONTEXT_MAX_TOKENS", "12000")),
            context_reserve_tokens=int(os.getenv("WEBCHAT_CONTEXT_RESERVE_TOKENS", "1200")),
            max_concurrent_runs=int(os.getenv("WEBCHAT_MAX_CONCURRENT_RUNS", "4")),
            max_user_runs=int(os.getenv("WEBCHAT_MAX_USER_RUNS", "1")),
            max_queue=int(os.getenv("WEBCHAT_MAX_QUEUE", "32")),
            queue_timeout=float(os.getenv("WEBCHAT_QUEUE_TIMEOUT_SECONDS", "30")),
            run_timeout=float(os.getenv("WEBCHAT_RUN_TIMEOUT_SECONDS", "180")),
            queue_url=(os.getenv("WEBCHAT_QUEUE_URL") or "").strip(),
            queue_name=(os.getenv("WEBCHAT_QUEUE_NAME") or "webchat:jobs").strip(),
            object_store=(os.getenv("WEBCHAT_OBJECT_STORE") or "local").strip(),
            s3_bucket=(os.getenv("WEBCHAT_S3_BUCKET") or "").strip(),
            s3_prefix=(os.getenv("WEBCHAT_S3_PREFIX") or "artifacts").strip(),
            s3_endpoint=(os.getenv("WEBCHAT_S3_ENDPOINT") or "").strip(),
            s3_region=(os.getenv("WEBCHAT_S3_REGION") or "").strip(),
        )
        settings.ensure_dirs()
        return settings

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.kb_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def agent_enabled(self) -> bool:
        return bool(self.agent_url)
