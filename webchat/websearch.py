"""Web research tools: search, fetch, source ranking and conflict detection.

The LLM never gets an arbitrary browser. It gets three bounded tools:
``web.search``, ``web.fetch`` and ``web.research``. Every URL is HTTPS-only and
SSRF-guarded; results carry source, timestamp and trust metadata.
"""

from __future__ import annotations

import difflib
import html
import ipaddress
import json
import os
import re
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

import httpx

MAX_FETCH_BYTES = 512_000
MAX_TEXT_CHARS = 12_000
MAX_RESULTS = 8
OFFICIAL_HOSTS = (
    "gov.cn", "gov", "edu", "edu.cn", "who.int", "un.org",
    "docs.python.org", "developer.mozilla.org", "github.com",
    "open-meteo.com", "sec.gov", "pbc.gov.cn", "sse.com.cn", "szse.cn",
)
TRUSTED_MEDIA = (
    "reuters.com", "apnews.com", "bbc.com", "xinhuanet.com", "people.com.cn",
    "chinadaily.com.cn", "thepaper.cn", "caixin.com",
)
LOW_QUALITY = ("contentfarm.example", "spam.example")


class WebSearchError(RuntimeError):
    pass


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    published_at: str = ""
    score: float = 0.0
    domain: str = ""


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str
    published_at: str = ""
    domain: str = ""
    content_type: str = ""


class _ArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.in_title = False
        self.meta: dict[str, str] = {}
        self.time_values: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            key = (attrs.get("property") or attrs.get("name") or "").lower()
            if key in {"article:published_time", "og:published_time", "date", "pubdate"}:
                self.meta[key] = attrs.get("content") or ""
        if tag == "time" and attrs.get("datetime"):
            self.time_values.append(attrs["datetime"])

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"} and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.skip:
            return
        text = data.strip()
        if not text:
            return
        if self.in_title:
            self.title_parts.append(text)
        else:
            self.text_parts.append(text)


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


def _allowed_host(host: str) -> bool:
    raw = os.getenv("WEBCHAT_WEB_ALLOW_HOSTS", "").strip()
    if not raw:
        return True
    allowed = [item.strip().lower() for item in raw.split(",") if item.strip()]
    host = host.lower()
    return any(host == item or host.endswith("." + item) for item in allowed)


def _published(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return value[:40]


class SearchProvider:
    name = "none"

    def search(self, query: str, *, freshness: str = "week", limit: int = 5) -> list[SearchResult]:
        raise NotImplementedError


class TavilySearch(SearchProvider):
    name = "tavily"

    def __init__(self, key: str) -> None:
        self.key = key

    def search(self, query: str, *, freshness: str = "week", limit: int = 5) -> list[SearchResult]:
        days = {"day": 1, "week": 7, "month": 30}.get(freshness, 7)
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.key,
                "query": query,
                "topic": "general",
                "days": days,
                "max_results": max(1, min(limit, MAX_RESULTS)),
                "search_depth": "basic",
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            raise WebSearchError(f"Tavily HTTP {resp.status_code}: {resp.text[:200]}")
        out = []
        for item in resp.json().get("results", []):
            url = str(item.get("url") or "")
            out.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=url,
                    snippet=str(item.get("content") or "")[:1200],
                    published_at=_published(str(item.get("published_date") or "")),
                    score=float(item.get("score") or 0),
                    domain=urlparse(url).hostname or "",
                )
            )
        return out


class SearxSearch(SearchProvider):
    name = "searxng"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, *, freshness: str = "week", limit: int = 5) -> list[SearchResult]:
        resp = httpx.get(
            self.base_url + "/search",
            params={
                "q": query,
                "format": "json",
                "language": "zh-CN",
                "time_range": {"day": "day", "week": "week", "month": "month"}.get(freshness, ""),
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            raise WebSearchError(f"SearXNG HTTP {resp.status_code}")
        out = []
        for item in resp.json().get("results", [])[: max(1, min(limit, MAX_RESULTS))]:
            url = str(item.get("url") or "")
            out.append(
                SearchResult(
                    title=str(item.get("title") or ""),
                    url=url,
                    snippet=str(item.get("content") or "")[:1200],
                    published_at=_published(str(item.get("publishedDate") or "")),
                    domain=urlparse(url).hostname or "",
                )
            )
        return out


class UnavailableSearch(SearchProvider):
    def search(self, query: str, *, freshness: str = "week", limit: int = 5) -> list[SearchResult]:
        raise WebSearchError(
            "未配置搜索 API：设置 TAVILY_API_KEY 或 WEBCHAT_SEARXNG_URL"
        )


def build_search_provider() -> SearchProvider:
    key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if key:
        return TavilySearch(key)
    base = (os.getenv("WEBCHAT_SEARXNG_URL") or "").strip()
    if base:
        return SearxSearch(base)
    return UnavailableSearch()


def fetch_page(url: str, *, timeout: float = 15) -> FetchedPage:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise WebSearchError("只允许抓取 https URL")
    explicit_allowlist = bool(os.getenv("WEBCHAT_WEB_ALLOW_HOSTS", "").strip())
    if not _allowed_host(parsed.hostname):
        raise WebSearchError(f"host 不在 allowlist：{parsed.hostname}")
    if not (explicit_allowlist and _allowed_host(parsed.hostname)):
        if not _is_public_host(parsed.hostname):
            raise WebSearchError("拒绝抓取私有/本地/保留地址")
    with httpx.stream(
        "GET",
        url,
        timeout=timeout,
        follow_redirects=False,
        headers={"User-Agent": "XinghuanChat/1.0 (+research)"},
    ) as resp:
        if resp.status_code >= 400:
            raise WebSearchError(f"HTTP {resp.status_code}")
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        chunks = []
        total = 0
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > MAX_FETCH_BYTES:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", resp.headers.get("content-type", ""), re.I)
    if match:
        charset = match.group(1)
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    if content_type == "application/json" or text.lstrip().startswith(("{", "[")):
        return FetchedPage(
            url=str(resp.url),
            title="",
            text=text[:MAX_TEXT_CHARS],
            domain=parsed.hostname,
            content_type=content_type or "application/json",
        )
    parser = _ArticleParser()
    parser.feed(text)
    title = " ".join(parser.title_parts).strip()
    body = "\n".join(parser.text_parts)
    body = re.sub(r"\n{3,}", "\n\n", body)[:MAX_TEXT_CHARS]
    published = next(iter(parser.meta.values()), "") or (parser.time_values[0] if parser.time_values else "")
    return FetchedPage(
        url=str(resp.url),
        title=html.unescape(title),
        text=body,
        published_at=_published(published),
        domain=parsed.hostname,
        content_type=content_type,
    )


def _host_tier(domain: str) -> tuple[int, float]:
    domain = (domain or "").lower()
    if not domain:
        return 4, 0.2
    if any(domain == h or domain.endswith("." + h) for h in OFFICIAL_HOSTS):
        return 1, 1.0
    if any(domain == h or domain.endswith("." + h) for h in TRUSTED_MEDIA):
        return 2, 0.75
    if any(domain == h or domain.endswith("." + h) for h in LOW_QUALITY):
        return 4, 0.05
    return 3, 0.45


def source_rank(results: list[SearchResult | dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in results:
        data = item.__dict__ if isinstance(item, SearchResult) else dict(item)
        domain = str(data.get("domain") or urlparse(str(data.get("url") or "")).hostname or "")
        counts[domain] = counts.get(domain, 0) + 1
        tier, authority = _host_tier(domain)
        raw_date = str(data.get("published_at") or "")
        recency = 0.4
        if raw_date:
            try:
                stamp = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                age_days = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400)
                recency = max(0.1, 1.0 / (1.0 + age_days / 7))
            except ValueError:
                pass
        search_score = min(1.0, max(0.0, float(data.get("score") or 0)))
        corroboration = min(1.0, counts[domain] / 2)
        trust = 0.45 * authority + 0.25 * recency + 0.15 * search_score + 0.10 * corroboration + 0.05
        ranked.append(
            {
                "title": data.get("title", ""),
                "url": data.get("url", ""),
                "snippet": data.get("snippet", ""),
                "published_at": raw_date,
                "domain": domain,
                "tier": tier,
                "trust_score": round(min(1.0, trust), 3),
                "recency": round(recency, 3),
            }
        )
    return sorted(ranked, key=lambda item: (-item["trust_score"], item["domain"]))


_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")


def conflict_detect(sources: list[dict[str, Any]]) -> dict[str, Any]:
    claims: list[dict[str, Any]] = []
    for source in sources:
        text = f"{source.get('title', '')} {source.get('snippet', '')}"
        for sentence in re.split(r"(?<=[。！？.!?])\s*", text):
            numbers = _NUMBER_RE.findall(sentence)
            if len(sentence) >= 12 and numbers:
                claims.append(
                    {
                        "text": sentence.strip()[:300],
                        "numbers": sorted(set(numbers)),
                        "domain": source.get("domain", ""),
                        "url": source.get("url", ""),
                    }
                )
    details = []
    for i, left in enumerate(claims):
        left_words = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", left["text"].lower()))
        for right in claims[i + 1 :]:
            right_words = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", right["text"].lower()))
            if not left_words or not right_words:
                continue
            overlap = len(left_words & right_words) / len(left_words | right_words)
            left_norm = re.sub(r"\d+(?:[.,]\d+)?", "#", left["text"])
            right_norm = re.sub(r"\d+(?:[.,]\d+)?", "#", right["text"])
            similarity = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
            if (overlap >= 0.45 or similarity >= 0.55) and left["numbers"] != right["numbers"]:
                details.append(
                    {
                        "left": left,
                        "right": right,
                        "overlap": round(overlap, 2),
                    }
                )
    return {"conflict": bool(details), "details": details[:5], "claims": claims[:30]}


class WebResearchService:
    def __init__(self, search: SearchProvider | None = None, fetcher=fetch_page) -> None:
        self.search = search or build_search_provider()
        self.fetcher = fetcher

    def search_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise WebSearchError("query 不能为空")
        freshness = str(args.get("freshness") or "week")
        limit = max(1, min(int(args.get("limit") or 5), MAX_RESULTS))
        results = self.search.search(query, freshness=freshness, limit=limit)
        items = []
        for index, result in enumerate(results, 1):
            item = result.__dict__
            item["index"] = index
            items.append(item)
        return {"results": items, "query": query}

    def fetch_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url") or "").strip()
        page = self.fetcher(url)
        return page.__dict__

    def research(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise WebSearchError("query 不能为空")
        freshness = str(args.get("freshness") or "week")
        limit = max(1, min(int(args.get("limit") or 4), 5))
        results = self.search.search(query, freshness=freshness, limit=limit)
        pages = []
        for result in results[:limit]:
            try:
                page = self.fetcher(result.url)
                pages.append(
                    {
                        "title": page.title or result.title,
                        "url": page.url,
                        "snippet": page.text[:1600],
                        "published_at": page.published_at or result.published_at,
                        "domain": page.domain or result.domain,
                        "text": page.text[:4000],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                pages.append(
                    {
                        "title": result.title,
                        "url": result.url,
                        "snippet": result.snippet,
                        "published_at": result.published_at,
                        "domain": result.domain,
                        "fetch_error": f"{type(exc).__name__}: {exc}",
                    }
                )
        ranked = source_rank(pages)
        for index, source in enumerate(ranked, 1):
            source["index"] = index
        conflicts = conflict_detect(ranked)
        return {
            "query": query,
            "sources": ranked,
            "conflicts": conflicts,
            "note": "使用来源中明确标注的发布时间；模型自身知识只用于理解问题，不作为实时证据。",
        }


def register_web_tools(tools: Any, settings: Any) -> None:
    service = WebResearchService()

    def run(fn):
        def handler(args: dict[str, Any]) -> dict[str, Any]:
            try:
                data = fn(args)
                return {"content": json.dumps(data, ensure_ascii=False)[:16000], "structured": data}
            except WebSearchError as exc:
                return {"content": f"联网查询失败：{exc}", "isError": True}

        return handler

    tools.register_local(
        name="web.search",
        description="搜索公开网页；返回标题、URL、摘要、发布时间。用于时效性问题。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "freshness": {"type": "string", "enum": ["day", "week", "month"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8},
            },
            "required": ["query"],
        },
        handler=run(service.search_tool),
        timeout=25,
    )
    tools.register_local(
        name="web.fetch",
        description="抓取一个 HTTPS 网页正文；仅用于已选定的搜索来源。",
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        handler=run(service.fetch_tool),
        timeout=20,
    )
    tools.register_local(
        name="web.research",
        description="搜索并抓取多个网页，做来源评分和冲突检测；用于最新新闻/天气/事实核查。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "freshness": {"type": "string", "enum": ["day", "week", "month"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["query"],
        },
        handler=run(service.research),
        timeout=45,
    )
