"""混合检索：向量召回 + 全文召回 + 图谱多跳 + 记忆召回，用 RRF 融合。

为什么不用单一向量检索：
  * 向量负责"意思相近"，但对专有名词、编号、精确短语不稳；
  * FTS(trigram) 负责"字面命中"，中文子串也能命中；
  * 图谱负责"关系推理"，能补上分块时被切断的跨段事实；
  * 记忆负责"关于人的长期信息"，与文档知识分开排序、分开引用。
RRF 不需要各分数可比，只依赖排名，天然鲁棒。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .memory import MemoryStore
from .ontology import Ontology
from .terms import TermsStore
from .textutil import content_units, embed_tokens, keyword_terms
from .util import clamp, truncate
from .vectors import KIND_CHUNK, load_matrix


@dataclass
class ChunkHit:
    chunk_id: str
    doc_id: str
    doc_title: str
    heading_path: str
    text: str
    score: float = 0.0
    vector_rank: int | None = None
    fts_rank: int | None = None
    graph_rank: int | None = None
    vector_score: float = 0.0
    fts_score: float = 0.0
    effective_date: str = ""
    doc_family: str = ""
    source_uri: str = ""

    @property
    def citation(self) -> str:
        path = f" › {self.heading_path}" if self.heading_path else ""
        return f"《{self.doc_title}》{path}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "heading_path": self.heading_path,
            "text": self.text,
            "score": round(self.score, 5),
            "ranks": {
                "vector": self.vector_rank,
                "fts": self.fts_rank,
                "graph": self.graph_rank,
            },
            "citation": self.citation,
            "effective_date": self.effective_date,
            "doc_family": self.doc_family,
            "source_uri": self.source_uri,
        }


@dataclass
class RetrievalResult:
    query: str
    hits: list[ChunkHit] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)
    seeds: list[str] = field(default_factory=list)
    unknown_terms: list[str] = field(default_factory=list)
    term_hits: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "hits": [h.to_dict() for h in self.hits],
            "entities": self.entities,
            "relations": self.edges,
            "memories": [
                {
                    "id": m["id"],
                    "kind": m["kind"],
                    "content": m["content"],
                    "score": m["score"],
                    "valid_from": m["valid_from"],
                }
                for m in self.memories
            ],
            "warnings": self.warnings,
        }


YEAR_MONTH_RE = re.compile(r"(20\d{2})\s*[年./-]\s*(\d{1,2})")
YEAR_RE = re.compile(r"(20\d{2})")
DOC_YEAR_SUFFIX_RE = re.compile(r"[\s_\-]*(?:19|20)\d{2}(?:\s*年)?$")
HISTORY_QUERY_RE = re.compile(r"历史|旧版|之前|以前|对比|变化|旧制度|上一版|20\d{2}\s*年")
POLICY_TITLE_RE = re.compile(r"制度|政策|规定|办法|规范")


def effective_date(title: str, text: str, source_uri: str = "", meta_json: str = "") -> str:
    try:
        meta = json.loads(meta_json or "{}")
    except (TypeError, ValueError):
        meta = {}
    for key in ("effective_date", "doc_date", "published_at"):
        value = str(meta.get(key) or "").strip()
        if value:
            match = YEAR_MONTH_RE.search(value) or YEAR_RE.search(value)
            if match:
                month = int(match.group(2)) if match.lastindex and match.lastindex >= 2 else 12
                return f"{match.group(1)}-{month:02d}"
    for value in (text[:240], title, source_uri):
        match = YEAR_MONTH_RE.search(value or "")
        if match:
            return f"{match.group(1)}-{int(match.group(2)):02d}"
        year = YEAR_RE.search(value or "")
        if year:
            return f"{year.group(1)}-12"
    return ""


def doc_family(title: str) -> str:
    value = re.sub(r"\.(md|markdown|txt|text|rst|pdf|docx|pptx|xlsx|xlsm|xls)$", "", title or "", flags=re.I)
    value = DOC_YEAR_SUFFIX_RE.sub("", value.strip())
    return re.sub(r"[\s_\-]+", "", value) or (title or "")


def _is_history_query(query: str) -> bool:
    return bool(HISTORY_QUERY_RE.search(query or ""))


def prefer_latest_policies(query: str, hits: list["ChunkHit"]) -> list["ChunkHit"]:
    if _is_history_query(query):
        return hits
    family_dates: dict[str, set[str]] = {}
    for hit in hits:
        if hit.effective_date:
            family_dates.setdefault(hit.doc_family, set()).add(hit.effective_date)
    latest = {
        family: max(dates) for family, dates in family_dates.items()
        if len(dates) > 1
    }
    if latest:
        hits = [
            hit for hit in hits
            if not hit.doc_family in latest
            or not hit.effective_date
            or hit.effective_date == latest[hit.doc_family]
        ]

    terms = [term for term in keyword_terms(query, min_len=2, limit=8) if len(term) >= 2]
    policy_docs = [
        hit for hit in hits
        if hit.effective_date and POLICY_TITLE_RE.search(hit.doc_title)
    ]
    matching_policy = [
        hit for hit in policy_docs
        if any(term in hit.doc_title for term in terms)
    ]
    if matching_policy:
        allowed = {hit.doc_id for hit in matching_policy}
        title_terms = {
            term for term in terms
            if any(term in hit.doc_title for hit in matching_policy)
        }
        other_terms = [term for term in terms if term not in title_terms]
        hits = [
            hit for hit in hits
            if hit.doc_id in allowed
            or any(term in hit.doc_title for term in title_terms)
            or any(term in hit.text for term in other_terms)
        ]
    return hits


class Retriever:
    def __init__(self, conn, config: dict, provider) -> None:
        self.conn = conn
        self.config = config
        self.provider = provider
        self.cfg = config.get("retrieval", {}) or {}
        self.ontology = Ontology(conn)
        self.memory = MemoryStore(conn, config, provider)
        self.terms = TermsStore(conn, config)
        self._vocab: set[str] | None = None

    # ------------------------------------------------------------ 三路召回

    def _vector_search(self, query: str, k: int) -> list[tuple[str, float]]:
        if getattr(self.conn, "is_postgres", False):
            q = self.provider.embedder.embed([query])
            if q.size == 0:
                return []
            literal = "[" + ",".join(f"{float(value):.8g}" for value in q[0]) + "]"
            rows = self.conn.execute(
                "SELECT owner_id, 1 - (vec <=> ?::vector) AS score FROM vectors"
                " WHERE owner_kind=? AND model=? AND dim=?"
                " ORDER BY vec <=> ?::vector LIMIT ?",
                (literal, KIND_CHUNK, self.provider.embedding_model, int(q.shape[1]), literal, k),
            ).fetchall()
            return [
                (row["owner_id"], float(row["score"]))
                for row in rows
                if float(row["score"]) > 0
            ]
        ids, matrix, dim = load_matrix(
            self.conn, KIND_CHUNK, model=self.provider.embedding_model
        )
        if not ids:
            return []
        q = self.provider.embedder.embed([query])
        if q.size == 0 or q.shape[1] != dim:
            return []
        scores = matrix @ q[0]
        top = np.argsort(-scores)[:k]
        return [(ids[i], float(scores[i])) for i in top if scores[i] > 0]

    def _fts_search(self, query: str, k: int) -> list[tuple[str, float]]:
        terms = keyword_terms(query, min_len=2, limit=24)
        if getattr(self.conn, "is_postgres", False):
            seen: set[str] = set()
            out: list[tuple[str, float]] = []
            for term in [t for t in terms if len(t) >= 2][:8]:
                rows = self.conn.execute(
                    "SELECT id FROM chunks WHERE text ILIKE ? LIMIT ?",
                    (f"%{term}%", k - len(out)),
                ).fetchall()
                for row in rows:
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    out.append((row["id"], 1.0))
                if len(out) >= k:
                    break
            return out[:k]
        out: list[tuple[str, float]] = []
        seen: set[str] = set()
        fts_terms = [t for t in terms if len(t) >= 3]  # trigram 至少 3 字符
        if fts_terms:
            match = " OR ".join(f'"{t}"' for t in fts_terms)
            try:
                rows = self.conn.execute(
                    "SELECT chunk_id, bm25(chunk_fts) AS rank FROM chunk_fts"
                    " WHERE chunk_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, k),
                ).fetchall()
                for r in rows:
                    if r["chunk_id"] in seen:
                        continue
                    seen.add(r["chunk_id"])
                    out.append((r["chunk_id"], float(-r["rank"])))
            except Exception:  # noqa: BLE001 - FTS 语法异常时退回 LIKE
                pass
        if len(out) < k:
            like_terms = [t for t in terms if len(t) >= 2][:8]
            for term in like_terms:
                rows = self.conn.execute(
                    "SELECT id FROM chunks WHERE text LIKE ? LIMIT ?",
                    (f"%{term}%", k - len(out)),
                ).fetchall()
                for r in rows:
                    if r["id"] in seen:
                        continue
                    seen.add(r["id"])
                    out.append((r["id"], 1.0))
                if len(out) >= k:
                    break
        return out[:k]

    def _graph_search(
        self, query: str, k: int, extra_seed_ids: Sequence[str] = ()
    ) -> tuple[list[dict], list[dict], list[tuple[str, float]], list[str]]:
        seeds = self.ontology.entities_in_text(query, limit=12)
        seed_ids = [s["entity_id"] for s in seeds]
        seed_ids += [sid for sid in extra_seed_ids if sid and sid not in seed_ids]
        if not seed_ids:
            return [], [], [], []
        hops = int(self.cfg.get("graph_hops", 2))
        sub = self.ontology.subgraph(
            seed_ids, hops=hops, min_weight=float(self.cfg.get("min_graph_weight", 0.35))
        )
        chunk_ids = self.ontology.chunk_ids_for_entities(seed_ids, limit=k)
        weighted = [(cid, 1.0 / (1 + i)) for i, cid in enumerate(chunk_ids)]
        return sub["nodes"], sub["edges"], weighted, seed_ids

    # ------------------------------------------------------------ 融合

    @staticmethod
    def _rrf(ranked_lists: Sequence[tuple[float, Sequence[str]]], k: int = 60) -> dict[str, float]:
        fused: dict[str, float] = {}
        for weight, ids in ranked_lists:
            for idx, cid in enumerate(ids):
                fused[cid] = fused.get(cid, 0.0) + weight / (k + idx + 1)
        return fused

    def search(
        self,
        query: str,
        *,
        k: int | None = None,
        include_history_memory: bool = False,
        include_episodes: bool = False,
        use_rerank: bool | None = None,
    ) -> RetrievalResult:
        query = (query or "").strip()
        k = int(k or self.cfg.get("top_k", 8))
        result = RetrievalResult(query=query)
        if not query:
            return result
        vk = int(self.cfg.get("vector_k", 40))
        fk = int(self.cfg.get("fts_k", 40))
        self._check_vector_model(result)

        # 术语层：只在检索式上做加法，原问题仍用于图谱种子与覆盖度判断
        search_query = query
        extra_seeds: list[str] = []
        if bool(self.cfg.get("use_terms", False)):
            search_query, term_hits = self.terms.expand(query)
            result.term_hits = term_hits
            result.coverage = self.terms.coverage(query)
            extra_seeds = [h["entity_id"] for h in term_hits if h.get("entity_id")]

        t0 = time.perf_counter()
        vec_hits = self._vector_search(search_query, vk)
        t1 = time.perf_counter()
        fts_hits = self._fts_search(search_query, fk)
        t2 = time.perf_counter()
        nodes, edges, graph_hits, seed_ids = self._graph_search(
            search_query, int(self.cfg.get("graph_k", 20)), extra_seeds
        )
        t3 = time.perf_counter()

        vec_ids = [c for c, _ in vec_hits]
        fts_ids = [c for c, _ in fts_hits]
        graph_ids = [c for c, _ in graph_hits]
        fused = self._rrf(
            [(1.0, vec_ids), (1.0, fts_ids), (0.8, graph_ids)],
            k=int(self.cfg.get("rrf_k", 60)),
        )
        if not fused:
            result.warnings.append("没有任何召回结果：知识库可能是空的，或向量/关键词索引未建立")
            return result

        candidates = sorted(fused.items(), key=lambda x: -x[1])[: max(k * 3, 24)]
        hydrated = self._hydrate([cid for cid, _ in candidates])
        vscore = dict(vec_hits)
        fscore = dict(fts_hits)
        vrank = {c: i + 1 for i, c in enumerate(vec_ids)}
        frank = {c: i + 1 for i, c in enumerate(fts_ids)}
        grank = {c: i + 1 for i, c in enumerate(graph_ids)}

        hits: list[ChunkHit] = []
        for cid, score in candidates:
            row = hydrated.get(cid)
            if row is None:
                continue
            hits.append(
                ChunkHit(
                    chunk_id=cid,
                    doc_id=row["doc_id"],
                    doc_title=row["doc_title"],
                    heading_path=row["heading_path"] or "",
                    text=row["text"],
                    score=score,
                    vector_rank=vrank.get(cid),
                    fts_rank=frank.get(cid),
                    graph_rank=grank.get(cid),
                    vector_score=vscore.get(cid, 0.0),
                    fts_score=fscore.get(cid, 0.0),
                    effective_date=effective_date(
                        row["doc_title"], row["text"], row["source_uri"], row["meta_json"]
                    ),
                    doc_family=doc_family(row["doc_title"]),
                    source_uri=row["source_uri"],
                )
            )

        hits = prefer_latest_policies(query, hits)

        do_rerank = bool(self.cfg.get("use_rerank", False)) if use_rerank is None else use_rerank
        if do_rerank and not self.provider.is_mock and len(hits) > 1:
            hits = self._llm_rerank(query, hits)

        hits.sort(key=lambda h: -h.score)
        result.hits = hits[:k]
        result.entities = nodes[:20]
        result.seeds = seed_ids
        result.unknown_terms = self._unknown_terms(query)
        result.edges = sorted(edges, key=lambda e: -float(e.get("weight", 0)))[:24]
        result.memories = self.memory.recall(
            query,
            k=int(self.cfg.get("memory_k", 6)),
            include_history=include_history_memory,
            include_episodes=include_episodes,
        )
        t4 = time.perf_counter()
        result.timings = {
            "vector_ms": round((t1 - t0) * 1000, 1),
            "fts_ms": round((t2 - t1) * 1000, 1),
            "graph_ms": round((t3 - t2) * 1000, 1),
            "total_ms": round((t4 - t0) * 1000, 1),
        }
        return result

    def _hydrate(self, chunk_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not chunk_ids:
            return {}
        ph = ",".join("?" for _ in chunk_ids)
        rows = self.conn.execute(
            f"""SELECT c.id, c.doc_id, c.heading_path, c.text, c.ord,
                       d.title AS doc_title, d.source_uri, d.meta_json,
                       d.created_at AS doc_created_at
                FROM chunks c JOIN documents d ON d.id = c.doc_id
                WHERE c.id IN ({ph})""",
            list(chunk_ids),
        ).fetchall()
        return {r["id"]: dict(r) for r in rows}

    def _vocabulary(self) -> set[str]:
        """语料里出现过的实字 / 英文词（进程内缓存）。

        只用于"问题是否超出知识边界"的判断。语料上到十万分块时应该换成
        持久化词典表，或直接改用向量相似度阈值。
        """
        if self._vocab is None:
            vocab: set[str] = set()
            for row in self.conn.execute("SELECT text FROM chunks").fetchall():
                vocab.update(content_units(row["text"]))
            self._vocab = vocab
        return self._vocab

    def _unknown_terms(self, query: str) -> list[str]:
        vocab = self._vocabulary()
        units = [unit for unit in content_units(query) if unit not in vocab]
        if units and bool(self.cfg.get("use_terms", False)):
            # 术语层的同义词算"已知说法"：SSO / MQ 不该被判成超纲词
            known = self.terms.known_units(units)
            units = [unit for unit in units if unit not in known]
        return units

    def _check_vector_model(self, result: RetrievalResult) -> None:
        rows = self.conn.execute(
            "SELECT DISTINCT model FROM vectors WHERE owner_kind=?", (KIND_CHUNK,)
        ).fetchall()
        models = {r["model"] for r in rows}
        if models and self.provider.embedding_model not in models:
            result.warnings.append(
                "已存向量模型（%s）与当前 provider（%s）不一致，向量检索会被跳过；"
                "请运行 `kb reindex --embeddings`"
                % (", ".join(sorted(models)), self.provider.embedding_model)
            )

    # ------------------------------------------------------------ LLM 重排

    def _llm_rerank(self, query: str, hits: list[ChunkHit]) -> list[ChunkHit]:
        batch = hits[:20]
        listing = "\n\n".join(
            f"[{i + 1}] {truncate(h.text, 350)}" for i, h in enumerate(batch)
        )
        prompt = (
            f"问题：{query}\n\n候选片段：\n{listing}\n\n"
            "请给每个片段与问题的相关性打分（0-10，只输出 JSON："
            '{"scores":[{"i":1,"score":7}]}），不要解释。'
        )
        try:
            raw = self.provider.chat.complete(
                "你是检索结果重排器，只输出 JSON。", prompt, json_mode=True, max_tokens=600
            )
            import json as _json

            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end <= start:
                return hits
            data = _json.loads(raw[start : end + 1])
            scores = {int(s["i"]): float(s["score"]) for s in data.get("scores", []) if "i" in s}
        except Exception:  # noqa: BLE001 - 重排失败就用原序
            return hits
        for i, hit in enumerate(batch, 1):
            if i in scores:
                hit.score = hit.score * 0.3 + clamp(scores[i] / 10.0, 0, 1) * 0.7
        return hits
