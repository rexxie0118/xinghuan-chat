"""Import an Agent/Skill folder from GitHub.

Only github.com URLs are accepted. We use the GitHub contents API (never an
arbitrary URL), so this is not a generic SSRF proxy. Public repos work without
a token; set GITHUB_TOKEN for private repos and higher rate limits.

Supported URL shapes:

    https://github.com/owner/repo
    https://github.com/owner/repo/tree/main/skills/pdf
    https://github.com/owner/repo/blob/main/skills/pdf/SKILL.md
    https://raw.githubusercontent.com/owner/repo/main/skills/pdf/SKILL.md

A folder should contain SKILL.md or AGENT.md (README.md is a fallback). The
importer includes text files up to four directory levels deep, max 250 files and
5MB total.
"""

from __future__ import annotations

import base64
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .agent_defs import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
)

API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
ARCHIVE_BASE = "https://codeload.github.com"
ARCHIVE_MAX_BYTES = 40 * 1024 * 1024
ARCHIVE_MAX_UNCOMPRESSED = 80 * 1024 * 1024
MANIFESTS = ("SKILL.md", "AGENT.md", "agent.md", "README.md")
SKIP_DIRS = {".git", ".github", "__pycache__", "node_modules", "dist", "build", "venv", ".venv"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar", ".woff", ".woff2", ".ttf"}
OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class GitHubImportError(ValueError):
    pass


@dataclass
class GitHubImportResult:
    markdown: str
    files: list[dict[str, str]] = field(default_factory=list)
    name: str = ""
    description: str = ""
    kind: str = "agent"
    source_url: str = ""
    source_ref: str = ""
    source_path: str = ""
    entrypoint: str = ""


@dataclass
class _RepoRef:
    owner: str
    repo: str
    ref: str = ""
    path: str = ""
    manifest: str = ""


def parse_github_url(url: str, ref: str | None = None, path: str | None = None) -> _RepoRef:
    raw = (url or "").strip()
    if not raw:
        raise GitHubImportError("GitHub URL 不能为空")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    parts = [p for p in parsed.path.split("/") if p]
    host = (parsed.hostname or "").lower()

    manifest = ""
    if host == "raw.githubusercontent.com":
        if len(parts) < 4:
            raise GitHubImportError("raw.githubusercontent.com 地址需要 owner/repo/ref/path")
        owner, repo = parts[0], parts[1]
        repo_ref = parts[2]
        file_path = "/".join(parts[3:])
        manifest = file_path
    elif host in {"github.com", "www.github.com"}:
        if len(parts) < 2:
            raise GitHubImportError("地址需要包含 owner/repo")
        owner, repo = parts[0], parts[1]
        repo_ref = ref or ""
        file_path = ""
        if len(parts) >= 4 and parts[2] in {"tree", "blob"}:
            repo_ref = parts[3]
            remainder = parts[4:]
            if parts[2] == "blob":
                manifest = "/".join(remainder)
                file_path = "/".join(remainder[:-1])
            else:
                file_path = "/".join(remainder)
    else:
        raise GitHubImportError("只支持 github.com / raw.githubusercontent.com 地址")

    if repo.endswith(".git"):
        repo = repo[:-4]
    if not OWNER_RE.match(owner) or not OWNER_RE.match(repo):
        raise GitHubImportError("非法 owner/repo")
    if ref:
        repo_ref = ref
    if path:
        file_path = path.strip("/")
        manifest = ""
    if manifest and manifest.lower().endswith((".md", ".markdown")):
        file_path = str(Path(manifest).parent).replace("\\", "/")
        if file_path == ".":
            file_path = ""
        manifest = Path(manifest).name
    return _RepoRef(owner=owner, repo=repo, ref=repo_ref, path=file_path.strip("/"), manifest=manifest)


class GitHubClient:
    def __init__(self, *, token: str | None = None) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "xinghuan-chat/0.1",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(timeout=20, headers=headers, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    def api(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self.client.get(f"{API_BASE}{path}", params=params)
        except httpx.HTTPError as exc:
            raise GitHubImportError(f"GitHub 请求失败：{type(exc).__name__}") from exc
        if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            raise GitHubImportError("GitHub API 速率限制，请稍后重试或配置 GITHUB_TOKEN")
        if resp.status_code == 404:
            raise GitHubImportError("GitHub 资源不存在，或仓库是私有的（需要 GITHUB_TOKEN）")
        if resp.status_code >= 400:
            raise GitHubImportError(f"GitHub API HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubImportError("GitHub 返回了非 JSON 内容") from exc

    def repo_info(self, owner: str, repo: str) -> dict[str, Any]:
        return self.api(f"/repos/{owner}/{repo}")

    def contents(self, owner: str, repo: str, path: str, ref: str) -> list[dict[str, Any]] | dict[str, Any]:
        return self.api(
            f"/repos/{owner}/{repo}/contents/{path.strip('/')}",
            params={"ref": ref},
        )


def _fetch_content(
    client: GitHubClient, owner: str, repo: str, ref: str, item: dict[str, Any]
) -> str:
    if item.get("content"):
        return _decode_file(item)
    path = str(item.get("path") or "")
    data = client.contents(owner, repo, path, ref)
    if not isinstance(data, dict):
        raise GitHubImportError(f"无法读取文件：{path}")
    return _decode_file(data)


def _decode_file(item: dict[str, Any]) -> str:
    content = item.get("content") or ""
    encoding = item.get("encoding")
    if encoding == "base64":
        try:
            raw = base64.b64decode(content)
        except Exception as exc:
            raise GitHubImportError(f"无法解码 {item.get('name')}") from exc
    else:
        raw = str(content).encode("utf-8")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitHubImportError(f"{item.get('name')} 不是文本文件") from exc


def _archive_bytes(owner: str, repo: str, ref: str, token: str | None) -> bytes | None:
    """Download a codeload zip; returns None when the ref/repo is not accessible."""
    headers = {"User-Agent": "xinghuan-chat/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for ref_kind in ("heads", "tags"):
        url = f"{ARCHIVE_BASE}/{owner}/{repo}/zip/refs/{ref_kind}/{ref}"
        try:
            with httpx.stream(
                "GET", url, headers=headers, timeout=60, follow_redirects=True
            ) as resp:
                if resp.status_code == 404:
                    continue
                if resp.status_code >= 400:
                    raise GitHubImportError(f"GitHub 归档下载 HTTP {resp.status_code}")
                data = bytearray()
                for chunk in resp.iter_bytes():
                    data.extend(chunk)
                    if len(data) > ARCHIVE_MAX_BYTES:
                        raise GitHubImportError("GitHub 仓库归档超过 40MB，已拒绝导入")
                return bytes(data)
        except httpx.HTTPError as exc:
            raise GitHubImportError(f"GitHub 归档下载失败：{type(exc).__name__}") from exc
    return None


def _try_archive(
    repo: _RepoRef,
    *,
    ref: str,
    entrypoint: str | None,
    token: str | None,
) -> GitHubImportResult | None:
    data = _archive_bytes(repo.owner, repo.repo, ref, token)
    if data is None:
        return None
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise GitHubImportError("GitHub 归档不是合法 zip") from exc
    names = zf.namelist()
    if not names:
        raise GitHubImportError("GitHub 归档为空")
    root = names[0].split("/", 1)[0]
    prefix = f"{root}/"
    base_prefix = f"{prefix}{repo.path.strip('/')}/" if repo.path else prefix
    manifest_name = repo.manifest
    if not manifest_name:
        candidates = []
        for info in zf.infolist():
            if not info.filename.startswith(base_prefix):
                continue
            rel = info.filename[len(base_prefix) :]
            if rel in MANIFESTS:
                candidates.append(rel)
        for candidate in MANIFESTS:
            if candidate in candidates:
                manifest_name = candidate
                break
    if not manifest_name:
        raise GitHubImportError("压缩包目录中没有 SKILL.md / AGENT.md / README.md")
    manifest_path = f"{base_prefix}{manifest_name}"
    try:
        markdown = zf.read(manifest_path).decode("utf-8")
    except (KeyError, UnicodeDecodeError) as exc:
        raise GitHubImportError(f"无法读取 {manifest_name}") from exc
    kind = "skill" if manifest_name.lower() == "skill.md" else "agent"

    files: list[dict[str, str]] = []
    total = 0
    for info in zf.infolist():
        if info.is_dir() or not info.filename.startswith(base_prefix):
            continue
        rel = info.filename[len(base_prefix) :]
        if not rel or rel == manifest_name:
            continue
        parts = Path(rel).parts
        if not parts or len(parts) > 4 or ".." in parts:
            continue
        if Path(rel).suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        if info.file_size > MAX_FILE_BYTES:
            continue
        if len(files) >= MAX_FILES:
            raise GitHubImportError(f"文件数超过 {MAX_FILES} 个")
        try:
            content = zf.read(info.filename).decode("utf-8")
        except UnicodeDecodeError:
            continue
        total += len(content.encode("utf-8"))
        if total > MAX_TOTAL_BYTES:
            raise GitHubImportError("文件总大小超过 5MB")
        files.append({"name": rel, "content": content})
    canonical = f"https://github.com/{repo.owner}/{repo.repo}"
    if repo.path:
        canonical += f"/tree/{ref}/{repo.path}"
    return GitHubImportResult(
        markdown=markdown,
        files=files,
        kind=kind,
        source_url=canonical,
        source_ref=ref,
        source_path=repo.path,
        entrypoint=(entrypoint or "").strip(),
    )


def import_from_github(
    url: str,
    *,
    ref: str | None = None,
    path: str | None = None,
    entrypoint: str | None = None,
    token: str | None = None,
) -> GitHubImportResult:
    repo = parse_github_url(url, ref=ref, path=path)
    token = token or os.getenv("GITHUB_TOKEN")
    if repo.ref:
        archived = _try_archive(repo, ref=repo.ref, entrypoint=entrypoint, token=token)
        if archived is not None:
            return archived
    else:
        for candidate in ("main", "master"):
            archived = _try_archive(repo, ref=candidate, entrypoint=entrypoint, token=token)
            if archived is not None:
                return archived
    client = GitHubClient(token=token)
    try:
        info = client.repo_info(repo.owner, repo.repo)
        repo_ref = repo.ref or str(info.get("default_branch") or "main")
        base = repo.path
        manifest_name = repo.manifest

        if base:
            listing = client.contents(repo.owner, repo.repo, base, repo_ref)
            if isinstance(listing, dict):
                raise GitHubImportError("URL 指向的是文件；请提供目录或 SKILL.md/AGENT.md 地址")
            names = {str(x.get("name")): x for x in listing if x.get("type") == "file"}
        else:
            listing = client.contents(repo.owner, repo.repo, "", repo_ref)
            names = {str(x.get("name")): x for x in listing} if isinstance(listing, list) else {}

        if not manifest_name:
            for candidate in MANIFESTS:
                if candidate in names:
                    manifest_name = candidate
                    break
            if not manifest_name:
                lowered = {k.lower(): k for k in names}
                for candidate in ("skill.md", "agent.md", "readme.md"):
                    if candidate in lowered:
                        manifest_name = lowered[candidate]
                        break
        if not manifest_name or manifest_name not in names:
            raise GitHubImportError(
                f"目录中没有 SKILL.md / AGENT.md / README.md：{base or repo.owner + '/' + repo.repo}"
            )

        manifest_item = names[manifest_name]
        markdown = _fetch_content(client, repo.owner, repo.repo, repo_ref, manifest_item)
        kind = "skill" if manifest_name.lower() == "skill.md" else "agent"

        files: list[dict[str, str]] = []
        total = 0

        def add_file(item: dict[str, Any], rel: str) -> None:
            nonlocal total
            name = str(item.get("name") or "")
            suffix = Path(name).suffix.lower()
            if suffix in SKIP_SUFFIXES or suffix not in ALLOWED_EXTENSIONS:
                return
            if name in MANIFESTS and (not rel or rel == name):
                return
            size = int(item.get("size") or 0)
            if size > MAX_FILE_BYTES:
                return
            if len(files) >= MAX_FILES:
                raise GitHubImportError(f"文件数超过 {MAX_FILES} 个")
            content = _fetch_content(client, repo.owner, repo.repo, repo_ref, item)
            total += len(content.encode("utf-8"))
            if total > MAX_TOTAL_BYTES:
                raise GitHubImportError("文件总大小超过 5MB")
            files.append({"name": rel, "content": content})

        for item in listing if isinstance(listing, list) else []:
            if item.get("type") == "file":
                add_file(item, str(item.get("name")))
            elif item.get("type") == "dir" and item.get("name") not in SKIP_DIRS:
                _walk_dir(client, repo.owner, repo.repo, repo_ref, base, str(item.get("name")), files, add_file, depth=1)
                if len(files) >= MAX_FILES:
                    raise GitHubImportError(f"文件数超过 {MAX_FILES} 个")

        canonical = f"https://github.com/{repo.owner}/{repo.repo}"
        if base:
            canonical += f"/tree/{repo_ref}/{base}"
        return GitHubImportResult(
            markdown=markdown,
            files=files,
            kind=kind,
            source_url=canonical,
            source_ref=repo_ref,
            source_path=base,
            entrypoint=(entrypoint or "").strip(),
        )
    finally:
        client.close()


def _walk_dir(
    client: GitHubClient,
    owner: str,
    repo: str,
    ref: str,
    base: str,
    directory: str,
    files: list[dict[str, str]],
    add_file,
    *,
    depth: int,
) -> None:
    if depth > 4:
        return
    rel_dir = f"{base}/{directory}".strip("/")
    listing = client.contents(owner, repo, rel_dir, ref)
    if not isinstance(listing, list):
        return
    for item in listing:
        name = str(item.get("name") or "")
        if item.get("type") == "file":
            add_file(item, f"{directory}/{name}")
        elif item.get("type") == "dir" and name not in SKIP_DIRS:
            _walk_dir(
                client,
                owner,
                repo,
                ref,
                base,
                f"{directory}/{name}",
                files,
                add_file,
                depth=depth + 1,
            )
