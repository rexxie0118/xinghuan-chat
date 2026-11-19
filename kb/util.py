"""通用工具：时间、ID、JSON、配置合并。全部基于标准库，无副作用。"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------- 时间


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """统一使用带时区的 ISO8601，秒级精度，便于排序与解析。"""
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_days(value: str | None, *, at: datetime | None = None) -> float:
    dt = parse_iso(value)
    if dt is None:
        return 0.0
    delta = (at or now()) - dt
    return max(0.0, delta.total_seconds() / 86400.0)


# ---------------------------------------------------------------- ID / 哈希


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def make_id(prefix: str, *parts: str) -> str:
    """确定性 ID：同样的输入永远得到同样的主键，天然幂等。"""
    raw = "\x1f".join(p or "" for p in parts)
    return f"{prefix}_{sha1(raw)[:16]}"


def content_hash(text: str) -> str:
    return sha1(text)


def hash_bucket(token: str, dim: int) -> tuple[int, int]:
    """把 token 映射到 (桶位, 符号)，用于无模型的特征哈希向量。"""
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(h, "big")
    return n % dim, (1 if (n >> 63) & 1 else -1)


# ---------------------------------------------------------------- 数值 / JSON


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def jd(obj: Any) -> str:
    """确定性 JSON dump，保证同样的对象产出同样的字符串。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def jl(raw: str | None, default: Any = None) -> Any:
    if not raw:
        return default if default is not None else {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default if default is not None else {}


def truncate(text: str, limit: int, ellipsis: str = "…") -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + ellipsis


def norm_score(x: float) -> float:
    """把 0~∞ 的分数压到 0~1，用于展示。"""
    return 1.0 - math.exp(-max(0.0, x))


# ---------------------------------------------------------------- 配置合并


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    """递归合并字典，override 优先；列表整体替换。"""
    out: dict = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], val)  # type: ignore[arg-type]
        else:
            out[key] = val
    return out


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in re.split(r"[,;\s]+", value) if p.strip()]


def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            return f"{x:.1f}{u}" if u != "B" else f"{int(x)}B"
        x /= 1024
    return f"{n}B"


def first_sentence(text: str, limit: int = 120) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"[。！？!?\n]", text)
    head = text[: m.start() + 1] if m else text
    return truncate(head, limit)


def uniq(items: Iterable[Any]) -> list[Any]:
    seen: set = set()
    out: list[Any] = []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


def days_ago(days: float) -> datetime:
    return now() - timedelta(days=days)
