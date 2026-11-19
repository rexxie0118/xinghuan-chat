"""记忆层：写入策略 + 时间衰减 + 冲突替代。

和"文档 RAG"的区别在于记忆有生命周期：
  * 去重：同一主体+谓词、同一内容反复出现，只累加命中次数（强化）；
  * 冲突：同一槽位出现不同值（"负责人是张三" → "负责人是李四"），
    旧值不删除，而是打上 valid_to 与 superseded_by，保留可审计的历史；
  * 衰减：salience = importance·e^(-λ·闲置天数) + 0.12·log(1+hits)，
    越久没被用到越容易被挤出召回窗口，被反复用到的记忆反而升权。

这套策略让知识库能"改主意"，而不是把矛盾事实都混进上下文里。
"""

from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np

from .textutil import embed_tokens, soft_key
from .util import age_days, clamp, iso, make_id
from .vectors import KIND_MEMORY, get_vector, put_vector

VALID_KINDS = ("fact", "preference", "episode", "task")


class MemoryStore:
    def __init__(self, conn, config: dict, provider=None) -> None:
        self.conn = conn
        self.cfg = (config or {}).get("memory", {}) or {}
        self.provider = provider

    # ------------------------------------------------------------ 工具

    def _embed(self, texts: Sequence[str]) -> np.ndarray | None:
        if self.provider is None:
            return None
        try:
            return self.provider.embedder.embed(list(texts))
        except Exception:  # noqa: BLE001 - 记忆写入不应因 embedding 失败而中断
            return None

    def _salience(self, row: dict[str, Any]) -> float:
        lam = float(self.cfg.get("decay_lambda", 0.01))
        last = row.get("last_access_at") or row.get("created_at")
        idle = age_days(last)
        base = float(row.get("importance", 0.5)) * math.exp(-lam * idle)
        bonus = 0.12 * math.log1p(float(row.get("hits", 0) or 0))
        return round(clamp(base + bonus, 0.0, 1.0), 4)

    # ------------------------------------------------------------ 写入

    def add(
        self,
        *,
        kind: str,
        content: str,
        subject: str = "",
        predicate: str = "",
        object_text: str = "",
        importance: float | None = None,
        confidence: float = 0.7,
        source_doc_id: str | None = None,
        source_chunk_id: str | None = None,
        subject_entity_id: str | None = None,
        valid_from: str | None = None,
    ) -> dict[str, Any]:
        if kind not in VALID_KINDS:
            raise ValueError(f"未知记忆类型：{kind}（支持 {VALID_KINDS}）")
        content = (content or "").strip()
        if not content:
            raise ValueError("记忆内容不能为空")
        importance = float(
            importance if importance is not None else self.cfg.get("base_importance", 0.5)
        )
        subject = (subject or "").strip()
        subject_norm = soft_key(subject)
        predicate = (predicate or "").strip()
        object_text = (object_text or content).strip()
        vec = self._embed([content])
        mem_vec = vec[0] if vec is not None and len(vec) else None

        memory_id = make_id("mem", kind, subject_norm, predicate, soft_key(object_text))
        ts = iso()

        existing = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if existing is not None:
            self.conn.execute(
                "UPDATE memories SET hits=hits+1, last_access_at=?, importance=MAX(importance,?)"
                " WHERE id=?",
                (ts, importance, memory_id),
            )
            return {"status": "duplicate", "id": memory_id, "superseded": []}

        superseded: list[str] = []
        if subject_norm and predicate:
            superseded = self._resolve_slot(
                kind=kind,
                subject_norm=subject_norm,
                predicate=predicate,
                object_text=object_text,
                new_vec=mem_vec,
                ts=ts,
                new_id=memory_id,
            )

        salience = self._salience(
            {"importance": importance, "created_at": ts, "hits": 0, "last_access_at": ts}
        )
        self.conn.execute(
            "INSERT INTO memories(id, kind, subject, subject_norm, subject_entity_id,"
            " predicate, object_text, content, importance, confidence, salience, hits,"
            " valid_from, valid_to, superseded_by, source_doc_id, source_chunk_id,"
            " created_at, last_access_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (memory_id, kind, subject, subject_norm, subject_entity_id, predicate,
             object_text, content, importance, confidence, salience, 0,
             valid_from or ts, None, None, source_doc_id, source_chunk_id, ts, ts),
        )
        if mem_vec is not None:
            put_vector(self.conn, KIND_MEMORY, memory_id, self.provider.embedding_model, mem_vec)
        status = "superseded" if superseded else "created"
        return {"status": status, "id": memory_id, "superseded": superseded}

    def _resolve_slot(
        self,
        *,
        kind: str,
        subject_norm: str,
        predicate: str,
        object_text: str,
        new_vec: np.ndarray | None,
        ts: str,
        new_id: str,
    ) -> list[str]:
        """处理同一 (主体, 谓词) 槽位上的重复与冲突。"""
        dedup = float(self.cfg.get("dedup_threshold", 0.92))
        conflict = float(self.cfg.get("conflict_threshold", 0.86))
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE valid_to IS NULL AND kind=? AND"
            " subject_norm=? AND predicate=?",
            (kind, subject_norm, predicate),
        ).fetchall()
        superseded: list[str] = []
        for row in rows:
            cand = dict(row)
            same_value = soft_key(cand["object_text"]) == soft_key(object_text)
            sim = 0.0
            if new_vec is not None:
                old_vec = get_vector(self.conn, KIND_MEMORY, cand["id"])
                if old_vec is not None and old_vec.shape == new_vec.shape:
                    sim = float(np.dot(new_vec, old_vec))
            if same_value or sim >= dedup:
                # 同一件事的另一种说法：并入旧记忆，不新增
                self.conn.execute(
                    "UPDATE memories SET hits=hits+1, last_access_at=?, confidence=MAX(confidence,?)"
                    " WHERE id=?",
                    (ts, cand["confidence"], cand["id"]),
                )
                superseded.append(cand["id"])
                continue
            if sim >= conflict:
                self.conn.execute(
                    "UPDATE memories SET valid_to=?, superseded_by=?, last_access_at=? WHERE id=?",
                    (ts, new_id, ts, cand["id"]),
                )
                superseded.append(cand["id"])
        return superseded

    # ------------------------------------------------------------ 召回

    def _row_to_dict(self, row) -> dict[str, Any]:
        item = dict(row)
        item["salience"] = self._salience(item)
        item["kind"] = item["kind"]
        return item

    def recall(
        self,
        query: str,
        *,
        k: int | None = None,
        include_history: bool = False,
        include_episodes: bool = False,
        reinforce: bool = True,
    ) -> list[dict[str, Any]]:
        k = int(k or self.cfg.get("recall_k", 6) or 6)
        clauses = [] if include_history else ["valid_to IS NULL"]
        if not include_episodes:
            # episode 是"我读过什么"的时间线，不参与事实类问答召回
            clauses.append("kind != 'episode'")
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self.conn.execute(f"SELECT * FROM memories WHERE {where}").fetchall()
        if not rows:
            return []
        qvec = None
        if self.provider is not None and (query or "").strip():
            emb = self._embed([query])
            qvec = emb[0] if emb is not None and len(emb) else None
        q_tokens = {t for t, _ in embed_tokens(query)}
        lam = float(self.cfg.get("decay_lambda", 0.01))

        scored: list[dict[str, Any]] = []
        for row in rows:
            item = self._row_to_dict(row)
            sim = 0.0
            if qvec is not None:
                mvec = get_vector(self.conn, KIND_MEMORY, item["id"])
                if mvec is not None and mvec.shape == qvec.shape:
                    sim = float(np.dot(qvec, mvec))
            toks = {t for t, _ in embed_tokens(item["content"])}
            if q_tokens and toks:
                lexical = len(q_tokens & toks) / len(q_tokens | toks)
                # 离线 mock 的向量本质是词法哈希，碰撞会带来虚假相似度，直接用词法分
                sim = lexical if (self.provider is not None and self.provider.is_mock) else max(sim, lexical)
            recency = math.exp(-lam * age_days(item.get("created_at")))
            score = 0.65 * sim + 0.25 * item["salience"] + 0.10 * recency
            item["score"] = round(score, 4)
            item["similarity"] = round(sim, 4)
            scored.append(item)

        scored.sort(key=lambda x: (-x["score"], -x["salience"]))
        top = scored[:k]
        if reinforce and top:
            ids = [m["id"] for m in top]
            ts = iso()
            self.conn.executemany(
                "UPDATE memories SET hits=hits+1, last_access_at=? WHERE id=?",
                [(ts, i) for i in ids],
            )
            for m in top:
                m["hits"] = int(m["hits"]) + 1
        return top

    # ------------------------------------------------------------ 管理

    def list(self, *, kind: str | None = None, include_expired: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        clauses = [] if include_expired else ["valid_to IS NULL"]
        params: list[Any] = []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        where = " AND ".join(clauses) if clauses else "1=1"
        rows = self.conn.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get(self, memory_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def forget(self, memory_id: str, *, hard: bool = False) -> bool:
        row = self.conn.execute("SELECT id FROM memories WHERE id=?", (memory_id,)).fetchone()
        if row is None:
            return False
        if hard:
            self.conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self.conn.execute(
                "DELETE FROM vectors WHERE owner_kind=? AND owner_id=?", (KIND_MEMORY, memory_id)
            )
        else:
            self.conn.execute(
                "UPDATE memories SET valid_to=?, superseded_by='forgotten' WHERE id=?",
                (iso(), memory_id),
            )
        return True

    def stats(self) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) c FROM memories WHERE valid_to IS NULL GROUP BY kind"
        ).fetchall()
        expired = self.conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE valid_to IS NOT NULL"
        ).fetchone()["c"]
        return {"active": {r["kind"]: int(r["c"]) for r in rows}, "superseded": int(expired)}

    def history(self, subject: str, predicate: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE subject_norm=? AND predicate=?"
            " ORDER BY valid_from",
            (soft_key(subject), predicate),
        ).fetchall()
        return [dict(r) for r in rows]
