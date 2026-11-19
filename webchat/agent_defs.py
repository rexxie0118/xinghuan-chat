"""Imported Agent definitions + a small, time-boxed script runner.

An Agent is a Markdown file plus reference scripts. The Markdown can declare a
frontmatter block:

    ---
    name: 发布检查
    entrypoint: check_release.py
    runtime: python
    timeout: 10
    ---

If there is no frontmatter, the first heading is the name and the first script
becomes the entrypoint. Scripts receive the user question as argv[1] and a JSON
payload on stdin (question, agent markdown, RAG context).

This is deliberately a *demo* executor, not a hard sandbox. It runs in a temp
directory with a scrubbed environment, a timeout, output limits and a process
group kill; it cannot stop a determined script from reading the host or using
the network. Put this behind a real container/job runner for production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import dump_json, load_json, new_id, utcnow

MAX_FILES = 20
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 1_000_000
MAX_RUNTIME_SECONDS = 15.0
ALLOWED_EXTENSIONS = {
    ".py", ".sh", ".js", ".mjs", ".ts", ".txt", ".json",
    ".md", ".yaml", ".yml", ".toml", ".csv",
}
RUNTIME_BY_EXT = {".py": "python", ".sh": "bash", ".js": "node", ".mjs": "node"}
EXECUTABLE_EXTENSIONS = set(RUNTIME_BY_EXT)
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-\u4e00-\u9fff]{1,120}$")


@dataclass
class ImportedAgent:
    id: str
    user_id: str
    name: str
    description: str
    markdown: str
    entrypoint: str
    runtime: str
    timeout: float
    files: list[dict[str, str]]
    created_at: str
    kind: str = "agent"
    source_url: str = ""
    source_ref: str = ""
    source_path: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "entrypoint": self.entrypoint,
            "runtime": self.runtime,
            "timeout": self.timeout,
            "files": [f["name"] for f in self.files],
            "created_at": self.created_at,
            "kind": self.kind,
            "source_url": self.source_url,
            "source_ref": self.source_ref,
            "source_path": self.source_path,
        }


def _clean_relpath(raw: str, *, executable: bool = False) -> str:
    """Allow safe relative paths such as ``scripts/run.py``.

    No absolute paths, ``..`` escapes, leading dots, or nested depths over 4.
    """
    text = str(raw or "").replace("\\", "/").strip()
    if not text or text.startswith("/"):
        raise ValueError(f"非法文件路径：{raw!r}")
    parts = [part for part in Path(text).parts if part not in ("", ".")]
    if not parts or len(parts) > 4:
        raise ValueError(f"文件路径层级过深：{raw!r}")
    if any(part == ".." or not SAFE_NAME.match(part) for part in parts):
        raise ValueError(f"非法文件路径：{raw!r}")
    name = "/".join(parts)
    if len(name) > 200:
        raise ValueError("文件路径过长")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"不支持的文件类型：{name}（允许 {', '.join(sorted(ALLOWED_EXTENSIONS))}）")
    if executable and suffix not in EXECUTABLE_EXTENSIONS:
        raise ValueError("entrypoint 必须是 .py / .sh / .js / .mjs 脚本")
    return name


def _frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    text = (markdown or "").replace("\r\n", "\n").strip()
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    end = None
    for index in range(1, min(len(lines), 80)):
        if lines[index].strip() in {"---", "..."}:
            end = index
            break
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line or line.strip().startswith("#"):
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, "\n".join(lines[end + 1 :]).strip()


def _infer_name(markdown: str, fallback: str = "") -> str:
    for line in (markdown or "").splitlines():
        line = line.strip()
        if line.startswith("# "):
            value = line[2:].strip()
            if value:
                return value[:60]
    return (fallback or "未命名 Agent")[:60]


def _infer_description(markdown: str) -> str:
    in_heading = False
    for line in (markdown or "").splitlines():
        line = line.strip()
        if line.startswith("#"):
            in_heading = True
            continue
        if in_heading and line and not line.startswith(("-", "*", ">", "```")):
            return line[:200]
    return ""


def build_agent(
    *,
    user_id: str,
    markdown: str,
    files: list[dict[str, str]],
    name: str | None = None,
    description: str | None = None,
    kind: str = "agent",
    source_url: str = "",
    source_ref: str = "",
    source_path: str = "",
    entrypoint_override: str | None = None,
) -> dict[str, Any]:
    markdown = (markdown or "").strip()
    if not markdown:
        raise ValueError("Agent Markdown 不能为空")
    if len(markdown.encode("utf-8")) > MAX_TOTAL_BYTES:
        raise ValueError("Agent Markdown 不能超过 1MB")
    if kind not in {"agent", "skill"}:
        raise ValueError("kind 必须是 agent 或 skill")
    if len(files) > MAX_FILES:
        raise ValueError(f"最多导入 {MAX_FILES} 个脚本")

    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    total = 0
    for raw in files:
        filename = _clean_relpath(raw.get("name", ""))
        if filename in seen:
            raise ValueError(f"重复的文件名：{filename}")
        seen.add(filename)
        content = str(raw.get("content") or "")
        if "\x00" in content:
            raise ValueError(f"{filename} 包含二进制内容")
        size = len(content.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise ValueError(f"{filename} 超过 256KB")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("脚本总大小不能超过 1MB")
        cleaned.append({"name": filename, "content": content})

    meta, body = _frontmatter(markdown)
    agent_name = (name or meta.get("name") or _infer_name(body, "未命名 Agent")).strip()[:60]
    if not agent_name:
        raise ValueError("Agent 名称不能为空")
    agent_description = (description or meta.get("description") or _infer_description(body)).strip()[:200]

    entrypoint = (entrypoint_override or meta.get("entrypoint") or "").strip()
    if entrypoint:
        entrypoint = _clean_relpath(entrypoint, executable=True)
    elif cleaned and kind == "agent":
        entrypoint = next(
            (
                f["name"]
                for f in cleaned
                if Path(f["name"]).suffix.lower() in EXECUTABLE_EXTENSIONS
            ),
            "",
        )
    names = {f["name"] for f in cleaned}
    if entrypoint and entrypoint not in names:
        raise ValueError(f"entrypoint {entrypoint!r} 不在上传的脚本列表中")

    runtime = (meta.get("runtime") or "").strip().lower()
    if not runtime and entrypoint:
        runtime = RUNTIME_BY_EXT.get(Path(entrypoint).suffix.lower(), "python")
    runtime = runtime or "python"
    if runtime not in {"python", "bash", "node"}:
        raise ValueError("runtime 只能是 python / bash / node")
    try:
        timeout = float(meta.get("timeout") or 10)
    except ValueError as exc:
        raise ValueError("timeout 必须是数字（秒）") from exc
    timeout = max(1.0, min(MAX_RUNTIME_SECONDS, timeout))

    return {
        "id": new_id("agt"),
        "user_id": user_id,
        "name": agent_name,
        "description": agent_description,
        "markdown": markdown,
        "entrypoint": entrypoint,
        "runtime": runtime,
        "timeout": timeout,
        "files_json": dump_json(cleaned),
        "created_at": utcnow().isoformat(timespec="microseconds"),
        "kind": kind,
        "source_url": source_url,
        "source_ref": source_ref,
        "source_path": source_path,
    }


def row_to_agent(row) -> ImportedAgent:
    return ImportedAgent(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row["description"],
        markdown=row["markdown"],
        entrypoint=row["entrypoint"],
        runtime=row["runtime"],
        timeout=float(row["timeout"]),
        files=load_json(row["files_json"], []),
        created_at=row["created_at"],
        kind=row["kind"] if "kind" in row.keys() else "agent",
        source_url=row["source_url"] if "source_url" in row.keys() else "",
        source_ref=row["source_ref"] if "source_ref" in row.keys() else "",
        source_path=row["source_path"] if "source_path" in row.keys() else "",
    )


def _public_from_row(row) -> dict[str, Any]:
    return row_to_agent(row).public()


def save_agent(conn, record: dict[str, Any]) -> dict[str, Any]:
    with conn:
        conn.execute(
            "INSERT INTO agents(id, user_id, name, description, markdown, entrypoint,"
            " runtime, timeout, files_json, created_at, kind, source_url, source_ref,"
            " source_path) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record["id"],
                record["user_id"],
                record["name"],
                record["description"],
                record["markdown"],
                record["entrypoint"],
                record["runtime"],
                record["timeout"],
                record["files_json"],
                record["created_at"],
                record.get("kind", "agent"),
                record.get("source_url", ""),
                record.get("source_ref", ""),
                record.get("source_path", ""),
            ),
        )
    return row_to_agent(
        conn.execute("SELECT * FROM agents WHERE id = ?", (record["id"],)).fetchone()
    ).public()


def list_agents(conn, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agents WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
    ).fetchall()
    return [_public_from_row(row) for row in rows]


def get_agent(conn, agent_id: str, user_id: str) -> ImportedAgent | None:
    row = conn.execute(
        "SELECT * FROM agents WHERE id = ? AND user_id = ?", (agent_id, user_id)
    ).fetchone()
    return row_to_agent(row) if row else None


def agent_detail(conn, agent_id: str, user_id: str) -> dict[str, Any] | None:
    agent = get_agent(conn, agent_id, user_id)
    if agent is None:
        return None
    data = agent.public()
    data["markdown"] = agent.markdown
    data["file_contents"] = agent.files
    return data


def delete_agent(conn, agent_id: str, user_id: str) -> bool:
    with conn:
        cur = conn.execute(
            "DELETE FROM agents WHERE id = ? AND user_id = ?", (agent_id, user_id)
        )
    return cur.rowcount > 0


def cited_indices(text: str, hit_count: int) -> list[int]:
    found: list[int] = []
    for match in re.finditer(r"\[(\d{1,2})\]", text or ""):
        index = int(match.group(1))
        if 1 <= index <= hit_count and index not in found:
            found.append(index)
    return found
