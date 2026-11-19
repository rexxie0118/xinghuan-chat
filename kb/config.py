"""配置加载：默认值 → JSON 文件 → 环境变量 → 显式覆盖，四层合并。"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .util import deep_merge

DEFAULT_CONFIG: dict[str, Any] = {
    "db_path": "data/kb.sqlite3",
    "provider": {
        "kind": "mock",  # mock | ark | openai | openai-compatible
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key_env": "ARK_API_KEY",
        "api_key": None,
        "chat_model": "doubao-seed-1-6-250615",
        "embedding_model": "doubao-embedding-large-text-250515",
        "embedding_dim": None,
        "timeout": 60,
        "max_retries": 3,
        "batch_size": 16,
    },
    "chunking": {"target_chars": 700, "max_chars": 1200, "overlap_chars": 120},
    "extraction": {
        "enabled": True,
        "min_confidence": 0.4,
        "max_chars_per_call": 1800,
        "link_mentions": True,
        "auto_register_types": True,
    },
    "retrieval": {
        "top_k": 8,
        "vector_k": 40,
        "fts_k": 40,
        "graph_hops": 2,
        "graph_k": 20,
        "memory_k": 6,
        "rrf_k": 60,
        "min_graph_weight": 0.35,
        "use_rerank": False,
    },
    "memory": {
        "dedup_threshold": 0.92,
        "conflict_threshold": 0.78,
        "decay_lambda": 0.01,
        "base_importance": 0.5,
    },
    "answer": {
        "max_context_chars": 7000,
        "min_score": 0.005,
        "language": "zh",
        "abstain_on_unknown_terms": True,
        "abstain_min_unknown": 2,
        "extractive_floor_ratio": 0.85,
    },
}

# 各后端的最小可用预设；配置里显式给出的字段仍然优先。
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "ark": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key_env": "ARK_API_KEY",
        "chat_model": "doubao-seed-1-6-250615",
        "embedding_model": "doubao-embedding-large-text-250515",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "chat_model": "gpt-4o-mini",
        "embedding_model": "text-embedding-3-small",
        "embedding_dim": 1536,
    },
}

# 环境变量映射：KB_PROVIDER / KB_DB / KB_CONFIG
ENV_PROVIDER = "KB_PROVIDER"
ENV_DB = "KB_DB"
ENV_CONFIG = "KB_CONFIG"


def _read_config_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # 明确报错，避免静默用错配置
        raise SystemExit(f"配置文件不是合法 JSON：{path}（{exc}）") from exc


def find_config_path(explicit: str | None = None, *, cwd: Path | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get(ENV_CONFIG)
    if env:
        return Path(env).expanduser()
    cur = cwd or Path.cwd()
    for candidate in (cur / "kb.config.json", cur / "kb.config.local.json"):
        if candidate.exists():
            return candidate
    return None


def load_config(
    path: str | None = None,
    *,
    provider: str | None = None,
    db_path: str | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """合并优先级：DEFAULT < provider 预设 < 配置文件 < 环境变量 < 显式参数。"""
    config = copy.deepcopy(DEFAULT_CONFIG)
    p = path if path is not None else os.environ.get(ENV_CONFIG)
    config_path = Path(p).expanduser() if p else None
    if config_path is None:
        config_path = find_config_path(None)
    file_cfg = _read_config_file(config_path) if config_path else {}
    config = deep_merge(config, file_cfg)

    kind = (
        provider
        or os.environ.get(ENV_PROVIDER)
        or config.get("provider", {}).get("kind")
        or "mock"
    )
    config["provider"]["kind"] = kind
    if kind in PROVIDER_PRESETS:
        preset = PROVIDER_PRESETS[kind]
        # 预设只填补“配置文件没写”的字段：先预设、再用配置文件覆盖回来
        merged = deep_merge(preset, config["provider"])
        merged["kind"] = kind
        config["provider"] = merged

    db = db_path or os.environ.get(ENV_DB) or config.get("db_path")
    config["db_path"] = db or DEFAULT_CONFIG["db_path"]

    if overrides:
        config = deep_merge(config, overrides)
    config["_config_path"] = str(config_path) if config_path else None
    return config


def api_key(config: Mapping[str, Any]) -> str | None:
    prov = config.get("provider", {})
    key = prov.get("api_key") or os.environ.get(prov.get("api_key_env") or "", "")
    key = (key or "").strip()
    return key or None


def db_path(config: Mapping[str, Any]) -> Path:
    return Path(str(config.get("db_path") or "data/kb.sqlite3")).expanduser()
