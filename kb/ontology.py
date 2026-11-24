"""类型化本体层：实体类型、关系类型、实体消解、多跳子图、图导出。

设计取舍（详见 DESIGN.md）：
  * 不引入 RDF/OWL，改用"类型 + 关系 + 少量 domain/range 约束"的轻量图谱，
    因为知识库的主要消费者是 LLM 与检索器，不是推理机；
  * schema 是开放的：遇到未知类型自动注册为 is_core=0，人工可审核升级，
    这样 LLM 抽取不会因为 schema 不全而丢信息；
  * 实体消解走"别名精确匹配 → 同名类型匹配 → 同类型模糊匹配"三级降级，
    命中的写法沉淀进 entity_aliases，后续匹配越来越准。
"""

from __future__ import annotations

import difflib
from typing import Any, Iterable, Sequence

from .textutil import normalize_name
from .util import iso, jd, make_id

ENTITY_KINDS = ["项目", "人物", "组织", "系统", "数据", "流程", "决策", "文档", "事件", "概念"]
MEMORY_KINDS = ["fact", "preference", "episode", "task"]


def _in(ids: Sequence[str]) -> str:
    return ",".join("?" for _ in ids)


class Ontology:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.violations = 0

    # ------------------------------------------------------------ 类型

    def ensure_entity_type(
        self, name: str, *, parent: str | None = None, description: str = "", is_core: int = 0
    ) -> str:
        name = (name or "").strip()
        if not name:
            name = "概念"
        row = self.conn.execute(
            "SELECT name FROM entity_types WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO entity_types(name, parent, description, is_core, created_at)"
                " VALUES(?,?,?,?,?)",
                (name, parent, description, is_core, iso()),
            )
        return name

    def ensure_relation_type(
        self,
        name: str,
        *,
        domain: str = "",
        range_: str = "",
        description: str = "",
        symmetric: bool = False,
        is_core: int = 0,
    ) -> str:
        name = (name or "").strip() or "相关"
        row = self.conn.execute(
            "SELECT name FROM relation_types WHERE name=?", (name,)
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO relation_types"
                "(name, domain, range, description, \"symmetric\", is_core, created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (name, domain, range_, description, int(symmetric), is_core, iso()),
            )
        return name

    def entity_type_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM entity_types ORDER BY is_core DESC, name"
        ).fetchall()
        return [r["name"] for r in rows]

    def relation_type_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM relation_types ORDER BY is_core DESC, name"
        ).fetchall()
        return [r["name"] for r in rows]

    def type_stats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT t.name, t.is_core, t.description,
                      COUNT(e.id) AS entities
               FROM entity_types t LEFT JOIN entities e ON e.type_name = t.name
               GROUP BY t.name ORDER BY entities DESC, t.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    def relation_type_stats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT t.name, t.is_core, t.description, t."symmetric",
                      COUNT(r.id) AS edges
               FROM relation_types t LEFT JOIN relations r ON r.type_name = t.name
               GROUP BY t.name ORDER BY edges DESC, t.name"""
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 实体

    def add_alias(self, entity_id: str, alias: str) -> None:
        norm = normalize_name(alias)
        if not norm or len(norm) < 1:
            return
        self.conn.execute(
            "INSERT INTO entity_aliases(entity_id, alias, alias_norm) VALUES(?,?,?)"
            " ON CONFLICT DO NOTHING",
            (entity_id, alias.strip(), norm),
        )

    def get_entity(self, entity_id: str):
        return self.conn.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()

    def find_entities_by_alias(self, name: str, *, type_name: str | None = None) -> list:
        norm = normalize_name(name)
        if not norm:
            return []
        sql = (
            "SELECT e.* FROM entity_aliases a JOIN entities e ON e.id = a.entity_id"
            " WHERE a.alias_norm = ?"
        )
        params: list[Any] = [norm]
        if type_name:
            sql += " AND e.type_name = ?"
            params.append(type_name)
        return self.conn.execute(sql, params).fetchall()

    def resolve_entity(
        self,
        name: str,
        type_name: str,
        *,
        aliases: Iterable[str] = (),
        description: str = "",
        confidence: float = 0.7,
        fuzzy_threshold: float = 0.86,
    ) -> tuple[str, bool]:
        """返回 (entity_id, 是否新建)。三级降级匹配，命中即并入。"""
        name = (name or "").strip()
        norm = normalize_name(name)
        if not norm:
            raise ValueError("实体名不能为空")
        type_name = self.ensure_entity_type(type_name)

        hit = self.find_entities_by_alias(name, type_name=type_name)
        if not hit:  # 类型不同也接受只读命中，避免同名分裂
            hit = self.find_entities_by_alias(name)
        if hit:
            entity = hit[0]
            self._bump(entity["id"], description=description, confidence=confidence)
            for alias in aliases:
                self.add_alias(entity["id"], alias)
            return entity["id"], False

        # 同类型下的模糊匹配：捕获 "统一登录服务" vs "统一登录" 这类写法差异
        candidates = self.conn.execute(
            "SELECT id, name FROM entities WHERE type_name=? AND substr(name_norm,1,1)=?",
            (type_name, norm[0]),
        ).fetchall()
        best: tuple[float, Any] | None = None
        for cand in candidates:
            ratio = difflib.SequenceMatcher(None, norm, normalize_name(cand["name"])).ratio()
            if ratio >= fuzzy_threshold and (best is None or ratio > best[0]):
                best = (ratio, cand)
        if best is not None:
            entity_id = best[1]["id"]
            self.add_alias(entity_id, name)
            for alias in aliases:
                self.add_alias(entity_id, alias)
            self._bump(entity_id, description=description, confidence=confidence)
            return entity_id, False

        entity_id = make_id("ent", type_name, norm)
        ts = iso()
        self.conn.execute(
            "INSERT INTO entities"
            "(id, type_name, name, name_norm, description, props_json, confidence,"
            " mention_count, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT DO NOTHING",
            (entity_id, type_name, name, norm, description, "{}", confidence, 1, ts, ts),
        )
        self.add_alias(entity_id, name)
        for alias in aliases:
            self.add_alias(entity_id, alias)
        return entity_id, True

    def _bump(self, entity_id: str, *, description: str = "", confidence: float = 0.7) -> None:
        if description:
            self.conn.execute(
                "UPDATE entities SET mention_count=mention_count+1, updated_at=?,"
                " description=CASE WHEN length(description) < length(?) THEN ? ELSE description END"
                " WHERE id=?",
                (iso(), description, description, entity_id),
            )
        else:
            self.conn.execute(
                "UPDATE entities SET mention_count=mention_count+1, updated_at=? WHERE id=?",
                (iso(), entity_id),
            )

    def entities_in_text(self, text: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """扫描文本里出现的已知实体别名（最长优先），用于图谱召回。"""
        norm = normalize_name(text)
        if not norm:
            return []
        rows = self.conn.execute(
            """SELECT a.alias_norm, a.alias, e.id, e.name, e.type_name
               FROM entity_aliases a JOIN entities e ON e.id = a.entity_id"""
        ).fetchall()
        found: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in sorted(rows, key=lambda r: -len(r["alias_norm"])):
            alias_norm = row["alias_norm"]
            if len(alias_norm) < 2 or alias_norm not in norm:
                continue
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            found.append(
                {
                    "entity_id": row["id"],
                    "name": row["name"],
                    "type": row["type_name"],
                    "surface": row["alias"],
                }
            )
            if len(found) >= limit:
                break
        return found

    def search_entities(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        norm = normalize_name(q)
        rows = self.conn.execute(
            """SELECT DISTINCT e.* FROM entities e
               LEFT JOIN entity_aliases a ON a.entity_id = e.id
               WHERE e.name_norm LIKE ? OR a.alias_norm LIKE ? OR e.name LIKE ?
               ORDER BY e.mention_count DESC LIMIT ?""",
            (f"%{norm}%", f"%{norm}%", f"%{q}%", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def merge_entities(self, source_id: str, target_id: str) -> dict[str, int]:
        """把重复节点并入目标节点：关系重连、别名与提及合并。"""
        if source_id == target_id:
            return {"relations": 0, "aliases": 0, "mentions": 0}
        moved = 0
        for row in self.conn.execute(
            "SELECT id, src_id, dst_id, type_name FROM relations"
            " WHERE src_id=? OR dst_id=?",
            (source_id, source_id),
        ).fetchall():
            src = target_id if row["src_id"] == source_id else row["src_id"]
            dst = target_id if row["dst_id"] == source_id else row["dst_id"]
            if src == dst:
                self.conn.execute("DELETE FROM relations WHERE id=?", (row["id"],))
                continue
            new_id = make_id("rel", row["type_name"], src, dst)
            try:
                self.conn.execute(
                    "UPDATE relations SET id=?, src_id=?, dst_id=? WHERE id=?",
                    (new_id, src, dst, row["id"]),
                )
            except Exception:  # 唯一冲突：目标边已存在，删掉这条
                self.conn.execute("DELETE FROM relations WHERE id=?", (row["id"],))
            moved += 1
        aliases = self.conn.execute(
            "UPDATE OR IGNORE entity_aliases SET entity_id=? WHERE entity_id=?",
            (target_id, source_id),
        ).rowcount
        self.conn.execute("DELETE FROM entity_aliases WHERE entity_id=?", (source_id,))
        mentions = self.conn.execute(
            "UPDATE OR IGNORE mentions SET entity_id=? WHERE entity_id=?", (target_id, source_id)
        ).rowcount
        self.conn.execute("DELETE FROM mentions WHERE entity_id=?", (source_id,))
        self.conn.execute("DELETE FROM entities WHERE id=?", (source_id,))
        return {"relations": moved, "aliases": int(aliases), "mentions": int(mentions)}

    # ------------------------------------------------------------ 关系

    def _check_constraint(self, rel_type: str, src_type: str, dst_type: str) -> bool:
        row = self.conn.execute(
            "SELECT domain, range FROM relation_types WHERE name=?", (rel_type,)
        ).fetchone()
        if row is None:
            return True
        ok = True
        if row["domain"] and row["domain"] != src_type:
            ok = False
        if row["range"] and row["range"] != dst_type:
            ok = False
        if not ok:
            self.violations += 1
        return ok

    def add_relation(
        self,
        src_id: str,
        rel_type: str,
        dst_id: str,
        *,
        evidence: str = "",
        weight: float = 1.0,
        confidence: float = 0.7,
        source_chunk_id: str | None = None,
    ) -> str:
        rel_type = self.ensure_relation_type(rel_type)
        if src_id == dst_id:
            raise ValueError("自环关系不写入")
        src = self.get_entity(src_id)
        dst = self.get_entity(dst_id)
        if src is None or dst is None:
            raise ValueError("关系端点不存在")
        if not self._check_constraint(rel_type, src["type_name"], dst["type_name"]):
            confidence = round(confidence * 0.6, 4)  # 违反 domain/range 只降权，不丢信息
        rel_id = make_id("rel", rel_type, src_id, dst_id)
        ts = iso()
        row = self.conn.execute("SELECT weight, confidence FROM relations WHERE id=?", (rel_id,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO relations(id, type_name, src_id, dst_id, weight, confidence,"
                " evidence, source_chunk_id, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (rel_id, rel_type, src_id, dst_id, weight, confidence,
                 evidence[:500], source_chunk_id, ts, ts),
            )
        else:
            # 重复出现的同一关系 = 证据增强（权重递增但有上限）
            self.conn.execute(
                "UPDATE relations SET weight=CASE WHEN weight + 0.25 > 3.0 THEN 3.0 ELSE weight + 0.25 END, updated_at=?,"
                " evidence=CASE WHEN length(evidence) < length(?) THEN ? ELSE evidence END"
                " WHERE id=?",
                (ts, evidence[:500], evidence[:500], rel_id),
            )
        sym = self.conn.execute(
            "SELECT \"symmetric\" FROM relation_types WHERE name=?", (rel_type,)
        ).fetchone()
        if sym and sym["symmetric"]:
            mirror = make_id("rel", rel_type, dst_id, src_id)
            self.conn.execute(
                "INSERT INTO relations(id, type_name, src_id, dst_id, weight,"
                " confidence, evidence, source_chunk_id, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (mirror, rel_type, dst_id, src_id, weight, confidence,
                 evidence[:500], source_chunk_id, ts, ts),
            )
        return rel_id

    def relations_of(self, entity_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT r.*, s.name AS src_name, s.type_name AS src_type,
                      d.name AS dst_name, d.type_name AS dst_type
               FROM relations r
               JOIN entities s ON s.id = r.src_id
               JOIN entities d ON d.id = r.dst_id
               WHERE r.src_id = ? OR r.dst_id = ?
               ORDER BY r.weight DESC LIMIT ?""",
            (entity_id, entity_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def subgraph(
        self,
        seed_ids: Sequence[str],
        *,
        hops: int = 2,
        min_weight: float = 0.35,
        max_nodes: int = 150,
    ) -> dict[str, Any]:
        """从种子实体做 BFS 扩展，返回 {nodes, edges}。用于多跳问答的证据补充。"""
        seeds = [s for s in dict.fromkeys(seed_ids) if s]
        if not seeds:
            return {"nodes": [], "edges": []}
        nodes: dict[str, dict[str, Any]] = {}
        edges: dict[str, dict[str, Any]] = {}
        frontier = set(seeds)
        for seed in seeds:
            row = self.get_entity(seed)
            if row:
                nodes[seed] = self._node(row)
        for _hop in range(max(0, hops)):
            if not frontier or len(nodes) >= max_nodes:
                break
            placeholders = _in(list(frontier))
            rows = self.conn.execute(
                f"""SELECT r.*, s.name AS src_name, s.type_name AS src_type,
                           d.name AS dst_name, d.type_name AS dst_type
                    FROM relations r
                    JOIN entities s ON s.id = r.src_id
                    JOIN entities d ON d.id = r.dst_id
                    WHERE (r.src_id IN ({placeholders}) OR r.dst_id IN ({placeholders}))
                      AND r.weight >= ?
                    ORDER BY r.weight DESC""",
                (*frontier, *frontier, min_weight),
            ).fetchall()
            nxt: set[str] = set()
            for row in rows:
                edges[row["id"]] = {
                    "id": row["id"],
                    "type": row["type_name"],
                    "src": row["src_id"],
                    "src_name": row["src_name"],
                    "dst": row["dst_id"],
                    "dst_name": row["dst_name"],
                    "weight": row["weight"],
                    "confidence": row["confidence"],
                    "evidence": row["evidence"],
                    "chunk_id": row["source_chunk_id"],
                }
                for eid, name, etype in (
                    (row["src_id"], row["src_name"], row["src_type"]),
                    (row["dst_id"], row["dst_name"], row["dst_type"]),
                ):
                    if eid not in nodes and len(nodes) < max_nodes:
                        nodes[eid] = {"id": eid, "name": name, "type": etype, "description": ""}
                    if eid not in frontier:
                        nxt.add(eid)
            frontier = nxt
        # 补全节点描述
        if nodes:
            ids = list(nodes)
            desc_rows = self.conn.execute(
                f"SELECT id, description, mention_count FROM entities WHERE id IN ({_in(ids)})",
                ids,
            ).fetchall()
            for r in desc_rows:
                nodes[r["id"]]["description"] = r["description"]
                nodes[r["id"]]["mentions"] = r["mention_count"]
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}

    @staticmethod
    def _node(row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "type": row["type_name"],
            "description": row["description"],
            "mentions": row["mention_count"],
        }

    def chunk_ids_for_entities(self, entity_ids: Sequence[str], *, limit: int = 40) -> list[str]:
        """实体直接证据：提及它的分块 + 以它为端点的关系来源分块。"""
        if not entity_ids:
            return []
        ids = list(dict.fromkeys(entity_ids))
        ph = _in(ids)
        rows = self.conn.execute(
            f"""SELECT chunk_id, COUNT(*) AS c FROM (
                    SELECT chunk_id FROM mentions WHERE entity_id IN ({ph})
                    UNION ALL
                    SELECT source_chunk_id AS chunk_id FROM relations
                     WHERE (src_id IN ({ph}) OR dst_id IN ({ph})) AND source_chunk_id IS NOT NULL
                ) GROUP BY chunk_id ORDER BY c DESC LIMIT ?""",
            (*ids, *ids, *ids, limit),
        ).fetchall()
        return [r["chunk_id"] for r in rows]

    # ------------------------------------------------------------ 统计 / 导出

    def stats(self) -> dict[str, Any]:
        ent = self.conn.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
        rel = self.conn.execute("SELECT COUNT(*) c FROM relations").fetchone()["c"]
        core_types = self.conn.execute(
            "SELECT COUNT(*) c FROM entity_types WHERE is_core=1"
        ).fetchone()["c"]
        pending = self.conn.execute(
            "SELECT COUNT(*) c FROM entity_types WHERE is_core=0"
        ).fetchone()["c"]
        top = self.conn.execute(
            """SELECT e.name, e.type_name, COUNT(r.id) AS degree
               FROM entities e LEFT JOIN relations r ON r.src_id=e.id OR r.dst_id=e.id
               GROUP BY e.id ORDER BY degree DESC LIMIT 5"""
        ).fetchall()
        return {
            "entities": ent,
            "relations": rel,
            "core_entity_types": core_types,
            "pending_entity_types": pending,
            "constraint_violations": self.violations,
            "top_hubs": [dict(r) for r in top],
        }

    def export(self, fmt: str = "json", *, limit: int = 5000) -> str:
        nodes = [
            dict(r)
            for r in self.conn.execute(
                "SELECT id, name, type_name, description, mention_count FROM entities"
                " ORDER BY mention_count DESC LIMIT ?",
                (limit,),
            )
        ]
        edges = [
            dict(r)
            for r in self.conn.execute(
                "SELECT type_name, src_id, dst_id, weight, evidence FROM relations"
                " ORDER BY weight DESC LIMIT ?",
                (limit,),
            )
        ]
        if fmt == "json":
            return jd({"nodes": nodes, "edges": edges})
        if fmt == "dot":
            lines = ["digraph ontology {", '  rankdir=LR; node [shape=box, fontsize=10];']
            for n in nodes:
                label = f"{n['name']}\\n({n['type_name']})"
                lines.append(f'  "{n["id"]}" [label="{label}"];')
            for e in edges:
                lines.append(
                    f'  "{e["src_id"]}" -> "{e["dst_id"]}" [label="{e["type_name"]}"];'
                )
            lines.append("}")
            return "\n".join(lines)
        raise ValueError(f"不支持的导出格式：{fmt}（json / dot）")

