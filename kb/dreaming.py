# -*- coding: utf-8 -*-
"""最小版 Dreaming：离线整理记忆，产出**草稿**，人工确认后才落地。

对应 Anthropic Dreaming 的安全模型：
    读（只读原库） → 整理（去重 / 合并 / 冲突取最新） → 写草稿表
    → 人看 diff → apply 才真正改 memories（旧行打 valid_to + superseded_by）

不做的事：
  * 不调用 LLM（确定性规则，可用 mock 环境跑，也能被测试固定）；
  * 不改原记忆的任何字段，除非显式 apply；
  * 会话记录来自 kind=episode 的记忆（ADK MemoryService 落的那种）。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from .textutil import normalize_name, soft_key
from .util import iso, make_id


class Dreamer:
    def __init__(self, conn, config: dict | None = None, provider=None) -> None:
        self.conn = conn
        self.cfg = (config or {}).get("dreaming", {}) or {}
        self.provider = provider

    # ------------------------------------------------------------ 读

    def active_memories(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memories WHERE valid_to IS NULL ORDER BY subject_norm, predicate, created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _slot(row: dict[str, Any]) -> str:
        subject = soft_key(row.get("subject") or "")
        predicate = soft_key(row.get("predicate") or "")
        if subject and predicate:
            return f"{subject}|{predicate}"
        return f"content|{normalize_name(row.get('content') or '')}"

    # ------------------------------------------------------------ 提议

    def propose(self) -> list[dict[str, Any]]:
        """生成草稿（覆盖本轮的 pending 草稿；已 applied/discarded 的保留）。"""
        self.conn.execute("DELETE FROM memories_draft WHERE status='pending'")
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in self.active_memories():
            groups.setdefault(self._slot(row), []).append(row)

        drafts = []
        handled: set[str] = set()
        for slot, rows in groups.items():
            if len(rows) < 2:
                continue
            drafts.append(self._draft_for(slot, rows))
            handled.update(r["id"] for r in rows)

        # 再补一层：跨槽位的语义近似（换序/同义写法），交给 LLM 复核
        drafts.extend(self._semantic_candidates(handled))

        for draft in drafts:
            self._save(draft)
        return drafts

    # ------------------------------------------------------------ 语义候选

    def _semantic_candidates(self, skip_ids: set[str]) -> list[dict[str, Any]]:
        """用现有 embedding 找「说法不同但可能是一件事」的记忆对。

        只产 review 草稿、不直接合并：相似度只是召回，是否同一事实交给 LLM 或人。
        """
        if self.provider is None:
            return []
        # mock 是特征哈希，换序同义句相似度偏低（~0.68）；真语义嵌入可以更严
        default = 0.65 if getattr(self.provider, "is_mock", True) else 0.85
        threshold = float(self.cfg.get("similarity_threshold", default))
        rows = [r for r in self.active_memories() if r["id"] not in skip_ids and r["kind"] != "episode"]
        if len(rows) < 2:
            return []
        try:
            matrix = self.provider.embedder.embed([r["content"] for r in rows])
        except Exception:  # noqa: BLE001 - embedding 不可用不影响确定性整理
            return []
        if matrix is None or len(matrix) != len(rows):
            return []

        drafts = []
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if self._slot(a) == self._slot(b):
                    continue                     # 同槽位已由 merge/supersede 处理
                sim = float(matrix[i] @ matrix[j])
                if sim < threshold:
                    continue
                keep, drop = (a, b) if (a["confidence"], a["importance"]) >= (b["confidence"], b["importance"]) else (b, a)
                drafts.append({
                    "action": "review",
                    "slot_key": self._slot(keep),
                    "kind": keep["kind"],
                    "keep_id": keep["id"],
                    "drop_ids": [drop["id"]],
                    "content": keep["content"],
                    "similarity": round(sim, 4),
                    "reason": f"语义近似：{keep['content'][:24]} ≈ {drop['content'][:24]}",
                })
        drafts.sort(key=lambda d: -d["similarity"])
        return drafts

    def _draft_for(self, slot: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_object: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = normalize_name(row.get("object_text") or row.get("content") or "")
            by_object.setdefault(key, []).append(row)

        # 同一事实的多种写法（"林晓" / "林晓（架构师）"）算重复：
        # 所有说法都是最长那条的子串时，保留信息最全的那条。
        longest = max(by_object, key=len)
        if len(by_object) == 1 or all(k in longest for k in by_object):
            def detail(r):
                return len(r.get("object_text") or "")

            keep = max(rows, key=lambda r: (detail(r), r["created_at"], r["confidence"]))
            drop = [r["id"] for r in rows if r["id"] != keep["id"]]
            return {
                "action": "merge",
                "slot_key": slot,
                "kind": keep["kind"],
                "keep_id": keep["id"],
                "drop_ids": drop,
                "content": keep["content"],
                "reason": f"重复：{len(rows)} 条记录说的是同一件事（保留最完整的一条：{keep['object_text'] or keep['content'][:20]}）",
            }

        # 同一槽位出现互斥说法 → 采用最新（并列时看置信度与命中次数）
        keep = max(rows, key=lambda r: (r["created_at"], r["confidence"], r["hits"]))
        drop = [r["id"] for r in rows if r["id"] != keep["id"]]
        return {
            "action": "supersede",
            "slot_key": slot,
            "kind": keep["kind"],
            "keep_id": keep["id"],
            "drop_ids": drop,
            "content": keep["content"],
            "reason": f"冲突：{len(by_object)} 种说法，采用最新（{keep['object_text'] or keep['content'][:30]}）",
        }

    def _save(self, draft: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO memories_draft(id, action, slot_key, kind, keep_id, drop_ids_json,
                   content, reason, similarity, reviewed, status, created_at)
               VALUES(?,?,?,?,?,?,?,?,?, '', 'pending', ?)""",
            (make_id("drf", draft["slot_key"], draft["keep_id"]), draft["action"], draft["slot_key"],
             draft.get("kind", ""), draft["keep_id"],
             json.dumps(draft["drop_ids"], ensure_ascii=False),
             draft["content"], draft["reason"], float(draft.get("similarity", 0.0)), iso()),
        )

    # ------------------------------------------------------------ 审阅

    def pending(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memories_draft WHERE status='pending' ORDER BY action, created_at"
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["drop_ids"] = json.loads(item.get("drop_ids_json") or "[]")
            item["drops"] = self._fetch(item["drop_ids"])
            item["keep"] = self._fetch([item["keep_id"]])[0] if item.get("keep_id") else None
            out.append(item)
        return out

    def _fetch(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        ph = ",".join("?" for _ in ids)
        rows = self.conn.execute(f"SELECT * FROM memories WHERE id IN ({ph})", list(ids)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 落地

    def review(self, draft_id: str) -> dict[str, Any]:
        """让 LLM 判断两条记忆是不是同一件事；mock 环境只标记为待人工确认。"""
        row = self.conn.execute(
            "SELECT * FROM memories_draft WHERE id=? AND status='pending'", (draft_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"没有待处理的草稿：{draft_id}")
        draft = dict(row)
        if draft["action"] != "review":
            return {"status": "skipped", "reason": "不是语义候选草稿"}

        if self.provider is None or getattr(self.provider, "is_mock", False):
            self.conn.execute(
                "UPDATE memories_draft SET reviewed='manual', reason=reason || '（mock 环境，待人工确认）' WHERE id=?",
                (draft_id,),
            )
            return {"status": "manual"}

        keep = self._fetch([draft["keep_id"]])[0]
        drop_ids = json.loads(draft["drop_ids_json"] or "[]")
        drop = self._fetch(drop_ids)[0]
        system = "你是记忆整理助手，只输出 JSON。"
        user = (
            "判断下面两条记忆是否在描述同一件事（可以合并为一条）。\n"
            "注意：只是主题相关不算同一件事；时间/数值/对象不同则不是。\n\n"
            f"A: {keep['content']}\nB: {drop['content']}\n\n"
            '只输出 JSON：{"same": true/false, "reason": "一句话理由"}'
        )
        try:
            raw = self.provider.chat.complete(system, user, json_mode=True, max_tokens=200)
            data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
            same = bool(data.get("same"))
            why = str(data.get("reason") or "")[:80]
        except Exception as exc:  # noqa: BLE001 - 复核失败退化为人工
            self.conn.execute(
                "UPDATE memories_draft SET reviewed='manual' WHERE id=?", (draft_id,)
            )
            return {"status": "manual", "reason": f"LLM 复核失败：{exc}"}

        if same:
            self.conn.execute(
                "UPDATE memories_draft SET reviewed='confirmed', reason=? WHERE id=?",
                (f"{draft['reason']} · 复核：{why}", draft_id),
            )
            return {"status": "confirmed", "reason": why}
        self.conn.execute(
            "UPDATE memories_draft SET status='discarded', reviewed='rejected', reason=? WHERE id=?",
            (f"{draft['reason']} · 复核否定：{why}", draft_id),
        )
        return {"status": "rejected", "reason": why}

    def review_all(self) -> list[dict[str, Any]]:
        return [self.review(d["id"]) for d in self.pending() if d["action"] == "review"]

    def apply(self, draft_id: str, *, force: bool = False) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM memories_draft WHERE id=? AND status='pending'", (draft_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"没有待处理的草稿：{draft_id}")
        if row["action"] == "review" and not force and row["reviewed"] != "confirmed":
            if row["reviewed"] == "manual":
                raise ValueError("待人工确认：mock 环境无法自动复核，确认后可用 apply --force")
            raise ValueError("语义候选还没复核：先 kb dream review --all，或 apply --force")
        draft = dict(row)
        drop_ids = json.loads(draft["drop_ids_json"] or "[]")
        now = iso()

        # 保留项：取各组里最高的置信度/重要性，并累计命中次数
        keep = self.conn.execute("SELECT * FROM memories WHERE id=?", (draft["keep_id"],)).fetchone()
        if keep is None:
            raise KeyError(f"待保留的记忆不存在：{draft['keep_id']}")
        dropped = self._fetch(drop_ids)
        confidence = max([keep["confidence"]] + [d["confidence"] for d in dropped])
        importance = max([keep["importance"]] + [d["importance"] for d in dropped])
        hits = sum(int(d["hits"] or 0) for d in dropped) + int(keep["hits"] or 0)
        self.conn.execute(
            "UPDATE memories SET confidence=?, importance=?, hits=?, last_access_at=? WHERE id=?",
            (confidence, importance, hits, now, draft["keep_id"]),
        )

        for drop in dropped:
            self.conn.execute(
                "UPDATE memories SET valid_to=?, superseded_by=? WHERE id=?",
                (now, draft["keep_id"], drop["id"]),
            )

        self.conn.execute(
            "UPDATE memories_draft SET status='applied', applied_at=? WHERE id=?", (now, draft_id)
        )
        return {"id": draft_id, "kept": draft["keep_id"], "superseded": drop_ids}

    def discard(self, draft_id: str) -> None:
        self.conn.execute(
            "UPDATE memories_draft SET status='discarded' WHERE id=? AND status='pending'", (draft_id,)
        )

    def discard_all(self) -> int:
        cur = self.conn.execute("UPDATE memories_draft SET status='discarded' WHERE status='pending'")
        return cur.rowcount or 0
