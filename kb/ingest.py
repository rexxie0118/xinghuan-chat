"""摄取管线：文件 → 文档 → 分块 → 向量 → 抽取 → 本体 → 记忆。

幂等性靠确定性 ID + content_hash：
同一路径重复摄取不会产生重复分块，内容未变时直接跳过（--force 可强制重算）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .db import meta_set
from .extraction import ExtractedMemory, Extraction, build_extractor
from .memory import MemoryStore
from .ontology import Ontology
from .textutil import chunk_markdown, normalize_text
from .util import content_hash, first_sentence, iso, jd, make_id
from .vectors import KIND_CHUNK, put_vectors

from .parsers import parse_file, supported_suffixes

SUPPORTED_SUFFIXES = supported_suffixes()

ProgressFn = Callable[[str], None]


@dataclass
class DocReport:
    source_uri: str
    title: str
    status: str  # created | updated | unchanged | error
    chunks: int = 0
    entities_new: int = 0
    entities_matched: int = 0
    relations: int = 0
    memories: int = 0
    detail: str = ""


@dataclass
class IngestReport:
    docs: list[DocReport] = field(default_factory=list)

    def add(self, doc: DocReport) -> None:
        self.docs.append(doc)

    @property
    def totals(self) -> dict[str, int]:
        keys = ["chunks", "entities_new", "entities_matched", "relations", "memories"]
        out = {k: sum(getattr(d, k) for d in self.docs) for k in keys}
        out["created"] = sum(1 for d in self.docs if d.status == "created")
        out["updated"] = sum(1 for d in self.docs if d.status == "updated")
        out["unchanged"] = sum(1 for d in self.docs if d.status == "unchanged")
        out["errors"] = sum(1 for d in self.docs if d.status == "error")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "totals": self.totals,
            "docs": [d.__dict__ for d in self.docs],
        }


def discover_files(paths: Sequence[str], *, recursive: bool = True) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            out.extend(
                sorted(p for p in path.glob(pattern) if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
            )
        elif path.is_file():
            out.append(path)
        else:
            raise FileNotFoundError(f"路径不存在：{raw}")
    # 去重且保持顺序
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(rp)
    return uniq


def _strip_heading_prefix(text: str, heading_path: str) -> str:
    """去掉分块正文开头的标题路径，避免标题被当成证据句。"""
    if heading_path and text.startswith(heading_path):
        rest = text[len(heading_path) :].lstrip("\n")
        if rest:
            return rest
    return text


class Ingester:
    def __init__(self, conn, config: dict, provider, *, progress: ProgressFn | None = None):
        self.conn = conn
        self.config = config
        self.provider = provider
        self.progress = progress or (lambda _msg: None)
        self.ontology = Ontology(conn)
        self.memory = MemoryStore(conn, config, provider)
        self.extractor = build_extractor(config, provider)
        self.chunk_cfg = config.get("chunking", {}) or {}
        self.ext_cfg = config.get("extraction", {}) or {}
        self.mem_cfg = config.get("memory", {}) or {}
        self.stats = {"entities_new": 0, "entities_matched": 0, "relations": 0, "memories": 0}

    # ------------------------------------------------------------ 入口

    def ingest_paths(
        self,
        paths: Sequence[str],
        *,
        doc_type: str | None = None,
        recursive: bool = True,
        force: bool = False,
    ) -> IngestReport:
        files = discover_files(paths, recursive=recursive)
        report = IngestReport()
        for idx, path in enumerate(files, 1):
            self.progress(f"[{idx}/{len(files)}] {path}")
            try:
                doc = self.ingest_file(path, doc_type=doc_type, force=force)
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不应终止整批
                doc = DocReport(
                    source_uri=str(path), title=path.name, status="error",
                    detail=f"{type(exc).__name__}: {exc}",
                )
                self._log(doc.source_uri, "error", doc.detail)
            report.add(doc)
        self._record_meta()
        return report

    def ingest_file(
        self, path: Path, *, doc_type: str | None = None, force: bool = False
    ) -> DocReport:
        parsed = parse_file(path)                 # PDF / DOCX / 文本统一在这里转换
        return self.ingest_text(
            parsed.text,
            title=parsed.title,
            source_uri=path.resolve().as_posix(),
            doc_type=doc_type or parsed.doc_type,
            force=force,
        )

    def ingest_text(
        self,
        text: str,
        *,
        title: str,
        source_uri: str,
        doc_type: str = "note",
        force: bool = False,
    ) -> DocReport:
        text = normalize_text(text)
        if not text:
            return DocReport(source_uri=source_uri, title=title, status="error", detail="内容为空")
        digest = content_hash(text)
        doc_id = make_id("doc", source_uri)
        existing = self.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

        if existing is not None and existing["content_hash"] == digest and not force:
            return DocReport(
                source_uri=source_uri, title=existing["title"], status="unchanged",
                chunks=self.conn.execute(
                    "SELECT COUNT(*) c FROM chunks WHERE doc_id=?", (doc_id,)
                ).fetchone()["c"],
            )

        is_update = existing is not None
        if is_update:
            self._delete_chunks(doc_id)

        ts = iso()
        conflict = "kb_id, id" if getattr(self.conn, "is_postgres", False) else "id"
        self.conn.execute(
            "INSERT INTO documents(id, source_uri, title, doc_type, content, content_hash,"
            " created_at, updated_at, meta_json) VALUES(?,?,?,?,?,?,?,?,?)"
            f" ON CONFLICT({conflict}) DO UPDATE SET title=excluded.title, doc_type=excluded.doc_type,"
            " content=excluded.content, content_hash=excluded.content_hash,"
            " updated_at=excluded.updated_at",
            (doc_id, source_uri, title, doc_type, text, digest, ts, ts, "{}"),
        )

        chunks = chunk_markdown(
            text,
            target_chars=int(self.chunk_cfg.get("target_chars", 700)),
            max_chars=int(self.chunk_cfg.get("max_chars", 1200)),
            overlap_chars=int(self.chunk_cfg.get("overlap_chars", 120)),
        )
        chunk_rows: list[tuple] = []
        for ch in chunks:
            chunk_id = make_id("chk", doc_id, str(ch["ord"]))
            chunk_rows.append(
                (chunk_id, doc_id, ch["ord"], ch["heading_path"], ch["text"],
                 ch["char_start"], ch["char_end"], ts)
            )
        self.conn.executemany(
            "INSERT INTO chunks(id, doc_id, ord, heading_path, text, char_start, char_end,"
            " created_at) VALUES(?,?,?,?,?,?,?,?)",
            chunk_rows,
        )
        self.conn.executemany(
            "INSERT INTO chunk_fts(text, chunk_id, doc_id) VALUES(?,?,?)",
            [(r[4], r[0], doc_id) for r in chunk_rows],
        )
        # 向量：正文前拼上标题，让"哪篇文档"也进入语义空间
        embed_inputs = [f"{title}\n{r[4]}" for r in chunk_rows]
        try:
            matrix = self.provider.embedder.embed(embed_inputs)
            put_vectors(
                self.conn, KIND_CHUNK, self.provider.embedding_model,
                [r[0] for r in chunk_rows], matrix,
            )
        except Exception as exc:  # noqa: BLE001 - 向量失败仍可用 FTS 检索
            self.progress(f"    ! 向量化失败，已降级为纯关键词检索：{exc}")

        report = DocReport(
            source_uri=source_uri, title=title,
            status="updated" if is_update else "created", chunks=len(chunk_rows),
        )

        if self.ext_cfg.get("enabled", True):
            extraction = self._extract(chunk_rows, title)
            self._apply_extraction(extraction, chunk_rows, doc_id, report)

        self._record_episode(title, text, doc_id, report)
        self._log(doc_id, report.status, "", {"chunks": report.chunks})
        return report

    # ------------------------------------------------------------ 抽取 & 落库

    def _extract(self, chunk_rows: list[tuple], title: str) -> Extraction:
        merged = Extraction()
        known = [
            r["name"]
            for r in self.conn.execute(
                "SELECT name FROM entities ORDER BY mention_count DESC LIMIT 40"
            ).fetchall()
        ]
        for row in chunk_rows:
            chunk_id, text = row[0], _strip_heading_prefix(row[4], row[3])
            try:
                part = self.extractor.extract(text, title=title, known_entities=known)
            except Exception as exc:  # noqa: BLE001 - 抽取失败保留已得结果
                merged.notes.append(f"{chunk_id}: {type(exc).__name__}: {exc}")
                continue
            merged.notes.extend(part.notes)
            for ent in part.entities:
                if ent.name not in {e.name for e in merged.entities}:
                    merged.entities.append(ent)
            merged.relations.extend(part.relations)
            merged.memories.extend(part.memories)
        return merged

    def _normalize_type(self, name: str) -> str:
        name = (name or "").strip() or "概念"
        known = self.conn.execute("SELECT name FROM entity_types WHERE name=?", (name,)).fetchone()
        if known:
            return name
        if not self.ext_cfg.get("auto_register_types", True):
            return "概念"
        return self.ontology.ensure_entity_type(name, description="由抽取自动发现，待人工审核")

    def _apply_extraction(
        self, extraction: Extraction, chunk_rows: list[tuple], doc_id: str, report: DocReport
    ) -> None:
        if extraction.empty:
            return
        id_by_name: dict[str, str] = {}
        type_by_name: dict[str, str] = {}
        for ent in extraction.entities:
            type_name = self._normalize_type(ent.type_name)
            try:
                entity_id, created = self.ontology.resolve_entity(
                    ent.name, type_name,
                    aliases=ent.aliases, description=ent.description,
                    confidence=ent.confidence,
                )
            except ValueError:
                continue
            id_by_name[ent.name] = entity_id
            type_by_name[ent.name] = type_name
            if created:
                report.entities_new += 1
            else:
                report.entities_matched += 1

        def chunk_for(evidence: str) -> str | None:
            """关系证据来自"去掉标题前缀"的正文，用子串匹配回填来源分块。"""
            key = (evidence or "").strip()[:20]
            if not key:
                return None
            for row in chunk_rows:
                if key in row[4]:
                    return row[0]
            return None

        for rel in extraction.relations:
            src_id = id_by_name.get(rel.src) or self._resolve_loose(rel.src, rel.src_type)
            dst_id = id_by_name.get(rel.dst) or self._resolve_loose(rel.dst, rel.dst_type)
            if not src_id or not dst_id or src_id == dst_id:
                continue
            source_chunk = chunk_for(rel.evidence)
            try:
                self.ontology.add_relation(
                    src_id, rel.rel_type, dst_id,
                    evidence=rel.evidence, confidence=rel.confidence,
                    weight=0.8, source_chunk_id=source_chunk,
                )
                report.relations += 1
            except ValueError:
                continue

        if self.ext_cfg.get("link_mentions", True):
            ts = iso()
            for name, entity_id in id_by_name.items():
                for row in chunk_rows:
                    if name and name in row[4]:
                        self.conn.execute(
                            "INSERT INTO mentions(id, entity_id, chunk_id, surface,"
                            " confidence, created_at) VALUES(?,?,?,?,?,?)"
                            " ON CONFLICT DO NOTHING",
                            (make_id("men", entity_id, row[0]), entity_id, row[0], name, 0.7, ts),
                        )

        for mem in extraction.memories:
            self._add_memory(mem, doc_id, report)

    def _resolve_loose(self, name: str, type_name: str) -> str | None:
        try:
            entity_id, created = self.ontology.resolve_entity(
                name, self._normalize_type(type_name), confidence=0.55
            )
            if created:
                self.stats["entities_new"] += 1
            return entity_id
        except ValueError:
            return None

    def _add_memory(self, mem: ExtractedMemory, doc_id: str, report: DocReport) -> None:
        subject_entity_id = None
        if mem.subject:
            try:
                subject_entity_id, _ = self.ontology.resolve_entity(
                    mem.subject, "人物" if mem.subject in {"我", "本人"} else "概念",
                    confidence=0.6,
                )
            except ValueError:
                subject_entity_id = None
        try:
            result = self.memory.add(
                kind=mem.kind, content=mem.content, subject=mem.subject,
                predicate=mem.predicate, object_text=mem.object_text,
                importance=mem.importance, confidence=mem.confidence,
                source_doc_id=doc_id, subject_entity_id=subject_entity_id,
            )
        except ValueError:
            return
        if result["status"] in {"created", "superseded"}:
            report.memories += 1
            self.stats["memories"] += 1

    def _record_episode(self, title: str, text: str, doc_id: str, report: DocReport) -> None:
        if not self.mem_cfg.get("record_episodes", True):
            return
        body = "\n".join(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        head = first_sentence(body or text, 160)
        result = self.memory.add(
            kind="episode",
            content=f"摄入了文档《{title}》：{head}",
            subject=title, predicate="主题",
            object_text=head, importance=0.35, confidence=0.5,
            source_doc_id=doc_id,
        )
        if result["status"] == "created":
            report.memories += 1

    # ------------------------------------------------------------ 清理 / 元数据

    def _delete_chunks(self, doc_id: str) -> None:
        rows = self.conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        self.conn.execute(f"DELETE FROM vectors WHERE owner_kind=? AND owner_id IN ({placeholders})",
                          (KIND_CHUNK, *ids))
        self.conn.execute(f"DELETE FROM chunk_fts WHERE chunk_id IN ({placeholders})", ids)
        self.conn.execute(f"DELETE FROM mentions WHERE chunk_id IN ({placeholders})", ids)
        self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))

    def _log(self, doc_id: str, status: str, detail: str = "", stats: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO ingest_log(id, doc_id, status, detail, stats_json, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (make_id("log", doc_id, iso(), str(len(doc_id))), doc_id, status,
             detail[:1000], jd(stats or {}), iso()),
        )

    def _record_meta(self) -> None:
        meta_set(self.conn, "embedding_model", self.provider.embedding_model)
        meta_set(self.conn, "provider_kind", self.provider.kind)
        meta_set(self.conn, "embedding_dim", str(self.provider.dim or ""))
        meta_set(self.conn, "last_ingest_at", iso())


def remove_document(conn, doc_id: str) -> int:
    """删除文档及其分块/向量/提及，保留已积累的实体关系（可能有其他证据支撑）。"""
    rows = conn.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,)).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        ph = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM vectors WHERE owner_kind=? AND owner_id IN ({ph})",
                     (KIND_CHUNK, *ids))
        conn.execute(f"DELETE FROM chunk_fts WHERE chunk_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM mentions WHERE chunk_id IN ({ph})", ids)
    conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
    n = conn.execute("DELETE FROM documents WHERE id=?", (doc_id,)).rowcount
    return int(n)
