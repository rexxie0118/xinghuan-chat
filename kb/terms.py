# -*- coding: utf-8 -*-
"""术语层（semantic layer）的最小实现。

对应 NVIDIA Auto Ontology 的「Generate → Certify」两阶段：

    seed_from_entities()  从已有实体生成**草稿**（certified=0）
    certify()             人工确认后置 1；只有认证过的 term 才参与检索增强

一个 term = 业务概念 + 描述 + 同义词 + 可选关联实体。它的作用是：
  * 消歧：同一概念的不同叫法收敛到一个名字（「SSO」「统一登录服务」）；
  * 扩展：检索前把问题里的术语补上同义词，缓解「问法和库里的说法不一致」；
  * 覆盖度：检索前问一句「这层语义到底覆不覆盖这个问题」。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Sequence

from .textutil import FUNCTION_CHARS, keyword_terms, normalize_name
from .util import iso, make_id


def _norms(values: Iterable[str]) -> list[str]:
    out = []
    for value in values:
        norm = normalize_name(value or "")
        if len(norm) >= 2:
            out.append(norm)
    return out


class TermsStore:
    def __init__(self, conn, config: dict | None = None) -> None:
        self.conn = conn
        self.cfg = (config or {}).get("terms", {}) or {}

    # ------------------------------------------------------------ 写入

    def add(
        self,
        name: str,
        *,
        description: str = "",
        synonyms: Sequence[str] = (),
        entity_id: str | None = None,
        kind: str = "concept",
        certified: bool = False,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("term 名称不能为空")
        norm = normalize_name(name)
        now = iso()
        row = self.conn.execute("SELECT id FROM terms WHERE name_norm=?", (norm,)).fetchone()
        if row:
            self.conn.execute(
                """UPDATE terms SET description=COALESCE(NULLIF(?,''), description),
                       synonyms_json=?, entity_id=COALESCE(?, entity_id),
                       certified=MAX(certified, ?), updated_at=?
                   WHERE id=?""",
                (description, json.dumps(list(synonyms), ensure_ascii=False),
                 entity_id, int(bool(certified)), now, row["id"]),
            )
            term_id = row["id"]
        else:
            term_id = make_id("term", norm)
            self.conn.execute(
                """INSERT INTO terms(id, name, name_norm, description, synonyms_json,
                       entity_id, kind, certified, usage_count, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,0,?,?)""",
                (term_id, name, norm, description,
                 json.dumps(list(synonyms), ensure_ascii=False),
                 entity_id, kind, int(bool(certified)), now, now),
            )
        return self.get(name)

    def seed_from_entities(self, *, limit: int = 40) -> dict[str, int]:
        """从实体生成草稿：描述取实体描述，同义词取别名（去掉实体名本身）。"""
        rows = self.conn.execute(
            """SELECT id, name, type_name, description, mention_count
                 FROM entities
                ORDER BY mention_count DESC, name ASC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        created = updated = 0
        for row in rows:
            existed = self.conn.execute(
                "SELECT 1 FROM terms WHERE name_norm=?", (normalize_name(row["name"]),)
            ).fetchone()
            aliases = [
                r["alias"]
                for r in self.conn.execute(
                    "SELECT alias FROM entity_aliases WHERE entity_id=? AND alias_norm<>?",
                    (row["id"], normalize_name(row["name"])),
                ).fetchall()
            ]
            self.add(
                row["name"],
                description=row["description"] or "",
                synonyms=aliases,
                entity_id=row["id"],
                kind=row["type_name"] or "concept",
                certified=False,
            )
            updated += 1 if existed else 0
            created += 0 if existed else 1
        return {"created": created, "updated": updated, "total": self.count()}

    def certify(self, name: str, *, description: str | None = None) -> dict[str, Any]:
        norm = normalize_name(name)
        row = self.conn.execute("SELECT id FROM terms WHERE name_norm=?", (norm,)).fetchone()
        if row is None:
            raise KeyError(f"没有这个术语：{name}")
        self.conn.execute(
            "UPDATE terms SET certified=1, description=COALESCE(?, description), updated_at=? WHERE id=?",
            (description, iso(), row["id"]),
        )
        return self.get(name)

    def certify_all(self) -> int:
        cur = self.conn.execute(
            "UPDATE terms SET certified=1, updated_at=? WHERE certified=0", (iso(),)
        )
        return cur.rowcount or 0

    # ------------------------------------------------------------ 读取

    def get(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM terms WHERE name_norm=?", (normalize_name(name),)
        ).fetchone()
        return self._row(row) if row else None

    def list(self, *, certified_only: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM terms"
        if certified_only:
            sql += " WHERE certified=1"
        sql += " ORDER BY certified DESC, usage_count DESC, name ASC"
        rows = self.conn.execute(sql + " LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def count(self, *, certified_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM terms" + (" WHERE certified=1" if certified_only else "")
        return int(self.conn.execute(sql).fetchone()["n"])

    @staticmethod
    def _row(row) -> dict[str, Any]:
        syn = json.loads(row["synonyms_json"] or "[]")
        return {
            "id": row["id"],
            "name": row["name"],
            "name_norm": row["name_norm"],
            "description": row["description"],
            "synonyms": syn,
            "entity_id": row["entity_id"],
            "kind": row["kind"],
            "certified": bool(row["certified"]),
            "usage_count": row["usage_count"],
        }

    # ------------------------------------------------------------ 匹配 / 扩展

    def match(self, text: str, *, certified_only: bool = False) -> list[dict[str, Any]]:
        """找出文本里出现的 term（名称或同义词，最长优先）。"""
        norm = normalize_name(text or "")
        if not norm:
            return []
        rows = self.conn.execute(
            "SELECT * FROM terms" + (" WHERE certified=1" if certified_only else "")
        ).fetchall()
        hits = []
        for row in rows:
            term = self._row(row)
            keys = [term["name"], *term["synonyms"]]
            for key in keys:
                k = normalize_name(key)
                if len(k) >= 2 and k in norm:
                    hits.append({**term, "matched": key, "len": len(k)})
                    break
        return sorted(hits, key=lambda h: -h["len"])

    def expand(self, query: str, *, max_terms: int = 4) -> tuple[str, list[dict[str, Any]]]:
        """把命中 term 的同义词/实体名补进检索式，缓解「问法与库内说法不一致」。"""
        hits = self.match(query, certified_only=True)[:max_terms]
        if not hits:
            return query, []
        query_norm = normalize_name(query)
        seen: set[str] = set()
        extra: list[str] = []
        for hit in hits:
            candidates = [hit["name"], *hit["synonyms"]]
            for cand in candidates:
                norm = normalize_name(cand)
                if len(norm) < 2 or norm in seen or norm in query_norm:
                    continue
                seen.add(norm)
                extra.append(cand)
        if not extra:
            return query, hits
        return f"{query} {' '.join(extra)}", hits

    def coverage(self, text: str, *, certified_only: bool = True) -> dict[str, Any]:
        """语义层覆盖度：问题里的实义词/二元组有多少能被 term 名称或同义词解释。

        用二元组而不是单字：中文不分词时，单字几乎必然被某个 term 包含，
        会让覆盖度虚高到接近 1。含疑问词/虚词的二元组（"哪些""的负"）先剔掉。
        """
        units = [
            u for u in keyword_terms(text or "", min_len=2, limit=24)
            if not (set(u) & FUNCTION_CHARS)
        ]
        rows = self.conn.execute(
            "SELECT * FROM terms" + (" WHERE certified=1" if certified_only else "")
        ).fetchall()
        keys: list[str] = []
        for row in rows:
            term = self._row(row)
            keys.extend(_norms([term["name"], *term["synonyms"]]))
        if not units:
            return {"ratio": 0.0, "total": 0, "covered": [], "missing": [], "matched_terms": []}

        covered, missing = [], []
        for unit in units:
            if any(unit in key for key in keys):
                covered.append(unit)
            else:
                missing.append(unit)
        matched = [h["name"] for h in self.match(text, certified_only=certified_only)]
        return {
            "ratio": round(len(covered) / len(units), 3),
            "total": len(units),
            "covered": covered,
            "missing": missing[:10],
            "matched_terms": matched,
        }

    def known_units(self, units: Iterable[str]) -> set[str]:
        """这些单元里哪些能被已认证 term 的名称/同义词解释（用于压 OOV 误判）。"""
        rows = self.conn.execute("SELECT * FROM terms WHERE certified=1").fetchall()
        keys: list[str] = []
        for row in rows:
            term = self._row(row)
            keys.extend(_norms([term["name"], *term["synonyms"]]))
        if not keys:
            return set()
        return {
            unit for unit in units
            if (norm := normalize_name(unit)) and any(norm in key for key in keys)
        }

    def check_answerable(self, text: str, *, threshold: float = 0.5) -> dict[str, Any]:
        cov = self.coverage(text)
        return {**cov, "answerable": cov["ratio"] >= threshold, "threshold": threshold}
