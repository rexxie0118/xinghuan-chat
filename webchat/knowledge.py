"""Knowledge-base facade: bootstrap the corpus, retrieve, and expose the graph.

The heavy lifting stays in the top-level ``kb`` package. This module only adapts
its sync API to the web app, keeps one provider instance, and shapes JSON for the
UI. It never lets a KB failure take down authentication or chat history.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, WORKSPACE

if str(WORKSPACE) not in sys.path:
    sys.path.insert(0, str(WORKSPACE))

LOG = logging.getLogger("webchat.kb")

from kb.answer import synthesize  # noqa: E402
from kb.config import load_config  # noqa: E402
from kb.db import connect, init_schema  # noqa: E402
from kb.ingest import Ingester, SUPPORTED_SUFFIXES, remove_document  # noqa: E402
from kb.ontology import Ontology  # noqa: E402
from kb.providers import Provider, ProviderError, build_provider  # noqa: E402
from kb.providers.mock import MockChat, MockEmbedder  # noqa: E402
from kb.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from kb.retrieval import Retriever  # noqa: E402

SAMPLE_QUESTIONS = ["这个知识库里有什么？"]


@dataclass(frozen=True)
class KBInfo:
    id: str
    name: str
    department: str
    description: str
    directory: str
    color: str
    sample_questions: tuple[str, ...]

    @property
    def is_shared(self) -> bool:
        return self.department == "all"


class KnowledgeService:
    def __init__(self, settings: Settings, info: KBInfo) -> None:
        self.settings = settings
        self.info = info
        self.kb_id = info.id
        self.name = info.name
        self.department = info.department
        self.knowledge_dir = (settings.root / info.directory).resolve()
        self.kb_path = settings.data_dir / f"kb_{info.id}.sqlite3"
        self._lock = threading.Lock()
        self.warning: str | None = None
        self.corpus: dict[str, Any] = {}
        self.cfg = load_config(
            str(settings.kb_config_path),
            db_path=str(self.kb_path),
            provider=settings.kb_provider,
        )
        try:
            if str(self.cfg.get("provider", {}).get("kind", "")).lower() == "deepseek":
                self.provider = self._deepseek_provider()
            else:
                self.provider = build_provider(self.cfg)
        except ProviderError as exc:
            LOG.warning("KB provider unavailable, falling back to mock: %s", exc)
            self.warning = f"知识库模型不可用，已降级为离线模式：{exc}"
            self.provider = Provider("mock", MockEmbedder(), MockChat())
        self._bootstrap()

    def _deepseek_provider(self) -> Provider:
        """DeepSeek for chat, local hash embeddings for retrieval.

        DeepSeek exposes an OpenAI-compatible /chat/completions endpoint but no
        embeddings endpoint. Keeping the existing local vectors means switching
        on a real answer model does not require a full KB reindex.
        """
        prov = self.cfg.get("provider", {}) or {}
        key = (
            os.getenv("DEEPSEEK_API_KEY")
            or os.getenv(str(prov.get("api_key_env") or "DEEPSEEK_API_KEY"))
            or str(prov.get("api_key") or "")
        ).strip()
        if not key:
            raise ProviderError(
                "WEBCHAT_KB_PROVIDER=deepseek 需要环境变量 DEEPSEEK_API_KEY"
            )
        impl = OpenAICompatProvider(
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
            api_key=key,
            chat_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"),
            embedding_model="mock-hash-256",
            timeout=float(prov.get("timeout") or 60),
            max_retries=int(prov.get("max_retries") or 3),
        )
        # Avoid spending extraction tokens during startup / ingestion. The rule
        # extractor still builds the ontology; chat is what needs the real model.
        self.cfg.setdefault("extraction", {})["enabled"] = False
        LOG.info("DeepSeek chat provider active (%s)", impl.chat_model)
        return Provider("deepseek", MockEmbedder(), impl)

    # ------------------------------------------------------------- bootstrap

    def _bootstrap(self) -> None:
        with self._lock:
            try:
                conn = connect(self.kb_path)
                try:
                    init_schema(conn)
                    exists = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
                    if not exists:
                        LOG.info("Empty KB %s, ingesting %s", self.kb_id, self.knowledge_dir)
                        self._ingest(conn, force=False)
                    elif self.knowledge_dir.exists():
                        # Cheap: unchanged docs are skipped by content hash. New
                        # files added to knowledge/ are picked up on restart.
                        self._ingest(conn, force=False)
                    self.corpus = self._corpus_counts(conn)
                finally:
                    conn.close()
                LOG.info("KB ready: %s", self.corpus)
            except Exception as exc:  # noqa: BLE001 - chat must still work
                LOG.exception("KB bootstrap failed")
                self.warning = f"知识库初始化失败：{type(exc).__name__}: {exc}"
                self.corpus = {}

    def _ingest(self, conn, *, force: bool) -> None:
        provider = self.provider
        ingestor = Ingester(conn, self.cfg, provider, progress=lambda msg: LOG.debug("ingest %s", msg))
        report = ingestor.ingest_paths([str(self.knowledge_dir)], force=force)
        LOG.info("Ingested corpus: %s", report.to_dict() if hasattr(report, "to_dict") else report)

    @staticmethod
    def _corpus_counts(conn) -> dict[str, Any]:
        def one(sql: str) -> int:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "entities": one("SELECT COUNT(*) FROM entities"),
            "relations": one("SELECT COUNT(*) FROM relations"),
            "memories": one("SELECT COUNT(*) FROM memories WHERE valid_to IS NULL"),
        }

    # --------------------------------------------------------------- helpers

    def _connection(self):
        return connect(self.kb_path)

    def _config_k(self, k: int | None = None) -> int:
        base = int(self.cfg.get("retrieval", {}).get("top_k", 6))
        return max(3, min(int(k or base), 12))

    @staticmethod
    def _graph_payload(nodes: list[dict], edges: list[dict]) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "type": n.get("type") or n.get("type_name") or "概念",
                    "description": (n.get("description") or "")[:220],
                    "mentions": n.get("mentions") or n.get("mention_count") or 0,
                }
                for n in nodes
            ],
            "edges": [
                {
                    "id": e["id"],
                    "type": e["type"],
                    "src": e["src"],
                    "dst": e["dst"],
                    "src_name": e.get("src_name", ""),
                    "dst_name": e.get("dst_name", ""),
                    "weight": round(float(e.get("weight") or 0), 3),
                    "evidence": (e.get("evidence") or "")[:280],
                }
                for e in edges
            ],
        }

    # -------------------------------------------------------------- retrieval

    def retrieve(self, question: str, *, k: int | None = None) -> dict[str, Any]:
        conn = self._connection()
        try:
            retriever = Retriever(conn, self.cfg, self.provider)
            result = retriever.search(question, k=self._config_k(k))
            graph = self._graph_for_result(conn, result)
            payload = result.to_dict()
            payload["seeds"] = list(result.seeds)
            payload["unknown_terms"] = list(result.unknown_terms)
            # Chat is grounded on documents + ontology; personal memories stay
            # out of the prompt so a demo answer is reproducible.
            payload["memories"] = []
            payload["graph"] = graph
            payload["stats"] = {
                "hits": len(result.hits),
                "entities": len(graph["nodes"]),
                "relations": len(graph["edges"]),
                "memories": len(result.memories),
                "timings": result.timings,
            }
            return payload
        finally:
            conn.close()

    def _graph_for_result(self, conn, result) -> dict[str, Any]:
        ontology = Ontology(conn)
        seeds = list(result.seeds or [])
        if not seeds:
            seeds = [e["id"] for e in result.entities[:5] if e.get("id")]
        if not seeds:
            return {"nodes": [], "edges": []}
        graph = ontology.subgraph(seeds, hops=2, min_weight=0.35, max_nodes=40)
        return self._graph_payload(graph["nodes"], graph["edges"])

    def answer(
        self,
        question: str,
        result: dict[str, Any],
        *,
        history: list[dict[str, str]] | None = None,
        memories: list[dict[str, Any]] | None = None,
    ):
        """Build the grounded answer. ``result`` is already a JSON-safe dict."""
        conn = self._connection()
        try:
            from kb.retrieval import ChunkHit, RetrievalResult

            hits = [
                ChunkHit(
                    chunk_id=h["chunk_id"],
                    doc_id=h["doc_id"],
                    doc_title=h["doc_title"],
                    heading_path=h.get("heading_path", ""),
                    text=h["text"],
                    score=float(h.get("score", 0)),
                )
                for h in result.get("hits", [])
            ]
            rr = RetrievalResult(query=question, hits=hits)
            rr.entities = result.get("entities", [])
            rr.relations = result.get("relations", [])
            rr.edges = result.get("relations", [])
            rr.memories = memories or []
            rr.seeds = result.get("seeds", [])
            rr.warnings = result.get("warnings", [])
            rr.timings = result.get("stats", {}).get("timings", {})
            return synthesize(question, rr, self.provider, self.cfg, history=history)
        finally:
            conn.close()

    # ---------------------------------------------------------------- graphs

    def global_graph(self, *, limit: int = 30) -> dict[str, Any]:
        conn = self._connection()
        try:
            ontology = Ontology(conn)
            hubs = conn.execute(
                """SELECT e.id, e.name, e.type_name, e.description, e.mention_count,
                          (SELECT COUNT(*) FROM relations r
                           WHERE r.src_id=e.id OR r.dst_id=e.id) AS degree
                   FROM entities e
                   ORDER BY degree DESC, e.mention_count DESC LIMIT 8"""
            ).fetchall()
            if not hubs:
                return {"nodes": [], "edges": []}
            graph = ontology.subgraph(
                [h["id"] for h in hubs], hops=1, min_weight=0.25, max_nodes=max(8, limit)
            )
            nodes = sorted(
                graph["nodes"],
                key=lambda n: (n.get("mentions") or 0, n.get("name") or ""),
                reverse=True,
            )[:limit]
            keep = {n["id"] for n in nodes}
            edges = [e for e in graph["edges"] if e["src"] in keep and e["dst"] in keep][:80]
            return self._graph_payload(nodes, edges)
        finally:
            conn.close()

    def graph_for_query(self, query: str) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return self.global_graph()
        conn = self._connection()
        try:
            ontology = Ontology(conn)
            candidates = ontology.entities_in_text(query, limit=8)
            for row in ontology.search_entities(query, limit=6):
                candidates.append(
                    {
                        "entity_id": row["id"],
                        "name": row["name"],
                        "type": row["type_name"],
                    }
                )
            ids = list(dict.fromkeys(c["entity_id"] for c in candidates))
            if not ids:
                return {"nodes": [], "edges": []}
            graph = ontology.subgraph(ids, hops=2, min_weight=0.3, max_nodes=35)
            return self._graph_payload(graph["nodes"], graph["edges"])
        finally:
            conn.close()

    def entity_detail(self, entity_id: str) -> dict[str, Any] | None:
        conn = self._connection()
        try:
            ontology = Ontology(conn)
            entity = ontology.get_entity(entity_id)
            if entity is None:
                return None
            relations = ontology.relations_of(entity_id, limit=40)
            chunk_ids = ontology.chunk_ids_for_entities([entity_id], limit=8)
            chunks = []
            if chunk_ids:
                ph = ",".join("?" for _ in chunk_ids)
                rows = conn.execute(
                    f"""SELECT c.id, c.text, c.heading_path, d.title
                        FROM chunks c JOIN documents d ON d.id = c.doc_id
                        WHERE c.id IN ({ph})""",
                    chunk_ids,
                ).fetchall()
                chunks = [
                    {
                        "chunk_id": r["id"],
                        "doc_title": r["title"],
                        "heading_path": r["heading_path"] or "",
                        "text": r["text"][:600],
                    }
                    for r in rows
                ]
            return {
                "id": entity["id"],
                "name": entity["name"],
                "type": entity["type_name"],
                "description": entity["description"] or "",
                "mentions": entity["mention_count"],
                "relations": [
                    {
                        "type": r["type_name"],
                        "direction": "out" if r["src_id"] == entity_id else "in",
                        "other": r["dst_name"] if r["src_id"] == entity_id else r["src_name"],
                        "other_id": r["dst_id"] if r["src_id"] == entity_id else r["src_id"],
                        "evidence": (r["evidence"] or "")[:300],
                    }
                    for r in relations
                ],
                "chunks": chunks,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------- overview

    def overview(self) -> dict[str, Any]:
        conn = self._connection()
        try:
            stats = self._corpus_counts(conn)
            types = [
                {"type": r["type_name"], "count": r["c"]}
                for r in conn.execute(
                    "SELECT type_name, COUNT(*) AS c FROM entities GROUP BY type_name"
                    " ORDER BY c DESC"
                ).fetchall()
            ]
            docs = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "doc_type": r["doc_type"],
                    "chunks": r["chunks"],
                    "updated_at": r["updated_at"],
                }
                for r in conn.execute(
                    """SELECT d.id, d.title, d.doc_type, d.updated_at,
                              (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.id) AS chunks
                       FROM documents d ORDER BY d.title"""
                ).fetchall()
            ]
            hubs = [
                {"id": r["id"], "name": r["name"], "type": r["type_name"], "degree": r["degree"]}
                for r in conn.execute(
                    """SELECT e.id, e.name, e.type_name,
                              (SELECT COUNT(*) FROM relations rel
                               WHERE rel.src_id=e.id OR rel.dst_id=e.id) AS degree
                       FROM entities e ORDER BY degree DESC LIMIT 6"""
                ).fetchall()
            ]
            return {
                "id": self.info.id,
                "name": self.info.name,
                "department": self.info.department,
                "description": self.info.description,
                "color": self.info.color,
                "sample_questions": list(self.info.sample_questions) or SAMPLE_QUESTIONS,
                "stats": stats,
                "types": types,
                "documents": docs,
                "hubs": hubs,
                "provider": {
                    "kind": self.provider.kind,
                    "chat_model": getattr(
                        self.provider.chat,
                        "chat_model",
                        getattr(self.provider.chat, "model", "unknown"),
                    ),
                    "embedding_model": self.provider.embedding_model,
                    "warning": self.warning,
                },
            }
        finally:
            conn.close()

    def documents(self) -> list[dict[str, Any]]:
        conn = self._connection()
        try:
            rows = conn.execute(
                """SELECT d.id, d.source_uri, d.title, d.doc_type, d.created_at,
                          d.updated_at,
                          (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.id) AS chunks
                   FROM documents d ORDER BY d.updated_at DESC, d.title"""
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "source_name": Path(row["source_uri"]).name,
                    "title": row["title"],
                    "doc_type": row["doc_type"],
                    "chunks": row["chunks"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def ingest_file(self, path: Path) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            try:
                ingestor = Ingester(
                    conn,
                    self.cfg,
                    self.provider,
                    progress=lambda msg: LOG.debug("ingest %s", msg),
                )
                report = ingestor.ingest_file(path)
                self.corpus = self._corpus_counts(conn)
                return report.__dict__
            finally:
                conn.close()

    def delete_document(self, doc_id: str) -> int:
        with self._lock:
            conn = self._connection()
            try:
                row = conn.execute(
                    "SELECT source_uri FROM documents WHERE id=?", (doc_id,)
                ).fetchone()
                if row is None:
                    return 0
                removed = remove_document(conn, doc_id)
                source = Path(row["source_uri"])
                try:
                    source.resolve().relative_to(self.knowledge_dir)
                    if source.is_file():
                        source.unlink()
                except (ValueError, OSError):
                    pass
                self.corpus = self._corpus_counts(conn)
                return removed
            finally:
                conn.close()

    def reindex(self) -> dict[str, Any]:
        with self._lock:
            conn = self._connection()
            try:
                before = self._corpus_counts(conn)
                self._ingest(conn, force=True)
                after = self._corpus_counts(conn)
                self.corpus = after
                return {"before": before, "after": after}
            finally:
                conn.close()

    def suggestions(self, question: str, result: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for entity in result.get("entities", [])[:4]:
            name = entity.get("name")
            if name and name not in question:
                out.append(f"「{name}」和哪些实体有关？")
        for sample in (self.info.sample_questions or SAMPLE_QUESTIONS):
            if sample != question and sample not in out:
                out.append(sample)
        return out[:3]


class KnowledgeRegistry:
    """All configured department knowledge bases, with user-level access rules."""

    def __init__(self, settings: Settings, config_path: Path | None = None) -> None:
        path = config_path or Path(
            os.getenv("WEBCHAT_KBS_CONFIG", settings.root / "kbs.json")
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        self.departments: list[str] = list(data.get("departments") or [])
        self.default_department: str = str(
            data.get("default_department")
            or (self.departments[0] if self.departments else "产品部")
        )
        self.infos: list[KBInfo] = []
        for raw in data.get("knowledge_bases") or []:
            self.infos.append(
                KBInfo(
                    id=str(raw["id"]),
                    name=str(raw.get("name") or raw["id"]),
                    department=str(raw.get("department") or "all"),
                    description=str(raw.get("description") or ""),
                    directory=str(raw.get("dir") or f"knowledge/{raw['id']}"),
                    color=str(raw.get("color") or "#8ca6ff"),
                    sample_questions=tuple(raw.get("sample_questions") or ()),
                )
            )
        if not self.infos:
            raise RuntimeError(f"知识库配置为空：{path}")
        self.services: dict[str, KnowledgeService] = {}
        for info in self.infos:
            self.services[info.id] = KnowledgeService(settings, info)
        LOG.info(
            "Knowledge registry ready: %s",
            {kb_id: svc.corpus.get("documents", 0) for kb_id, svc in self.services.items()},
        )

    def get(self, kb_id: str) -> KnowledgeService | None:
        return self.services.get(kb_id)

    def all(self) -> list[KnowledgeService]:
        return [self.services[info.id] for info in self.infos]

    def for_user(self, user: dict[str, Any]) -> list[KnowledgeService]:
        role = str(user.get("role") or "member")
        if role == "admin":
            return self.all()
        department = str(user.get("department") or self.default_department)
        return [
            svc
            for svc in self.all()
            if svc.info.is_shared or svc.info.department == department
        ]

    def default_for(self, user: dict[str, Any]) -> KnowledgeService | None:
        allowed = self.for_user(user)
        department = str(user.get("department") or self.default_department)
        for svc in allowed:
            if svc.info.department == department:
                return svc
        for svc in allowed:
            if svc.info.is_shared:
                return svc
        return allowed[0] if allowed else None

    @staticmethod
    def _summary(svc: KnowledgeService) -> dict[str, Any]:
        return {
            "id": svc.info.id,
            "name": svc.info.name,
            "department": svc.info.department,
            "description": svc.info.description,
            "color": svc.info.color,
            "documents": svc.corpus.get("documents", 0),
            "entities": svc.corpus.get("entities", 0),
            "relations": svc.corpus.get("relations", 0),
        }

    def catalog(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        return [self._summary(svc) for svc in self.for_user(user)]

    def catalog_public(self) -> list[dict[str, Any]]:
        return [
            {
                "id": info.id,
                "name": info.name,
                "department": info.department,
                "description": info.description,
                "color": info.color,
            }
            for info in self.infos
        ]
