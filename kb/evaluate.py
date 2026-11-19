"""评测：用标注集回答"这次改动到底变好了没有"。

评测集是一行一个 JSON 的 golden set，每题可标注四种 ground truth：

    expected_chunks    精确到分块 id（自动生成草稿时用）
    expected_docs      文档标题（人工标注时更稳）
    expected_sections  小节名，命中 heading_path 子串即可
    answer_terms       答案里必须出现的词（人名、数字、专有名词）
    relations          [src, 关系, dst] 三元组，检验图谱是否召回
    must_not           出现即判错的词（用于"别编造"类检查）

指标分四组，缺哪组 ground truth 就自动跳过哪组，不会用 0 值污染平均：

    检索   hit@k / MRR / nDCG@k
    答案   term_hit / citation_precision / citation_coverage / violation
    图谱   relation_recall
    拒答   negative 题是否真的说"没找到"

单次评测只跑一遍检索 + 一次答案生成，延迟与成本都可控（默认离线 mock 免费）。
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .answer import Answer, synthesize
from .retrieval import ChunkHit, RetrievalResult, Retriever
from .textutil import embed_tokens
from .util import iso, truncate

CASE_KINDS = ("fact", "relation", "multi_hop", "preference", "decision", "negative")
ABSTAIN_MARKERS = (
    "没有检索到", "没有找到", "没有相关", "未提及", "无法回答", "证据不足",
    "不确定", "没有足够", "知识库里没有",
)


# ---------------------------------------------------------------- 数据模型


@dataclass
class EvalCase:
    id: str
    question: str
    kind: str = "fact"
    expected_chunks: list[str] = field(default_factory=list)
    expected_docs: list[str] = field(default_factory=list)
    expected_sections: list[str] = field(default_factory=list)
    answer_terms: list[str] = field(default_factory=list)
    must_not: list[str] = field(default_factory=list)
    relations: list[list[str]] = field(default_factory=list)
    notes: str = ""
    draft: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "kind": self.kind,
            "expected_chunks": self.expected_chunks,
            "expected_docs": self.expected_docs,
            "expected_sections": self.expected_sections,
            "answer_terms": self.answer_terms,
            "must_not": self.must_not,
            "relations": self.relations,
            "notes": self.notes,
            "draft": self.draft,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, lineno: int | None = None) -> "EvalCase":
        where = f"第 {lineno} 行" if lineno else "记录"
        if not isinstance(data, dict):
            raise ValueError(f"{where}不是 JSON 对象")
        question = str(data.get("question") or "").strip()
        if not question:
            raise ValueError(f"{where}缺少 question")
        kind = str(data.get("kind") or "fact").strip()
        if kind not in CASE_KINDS:
            raise ValueError(f"{where}的 kind={kind} 不认识（可选 {', '.join(CASE_KINDS)}）")
        relations: list[list[str]] = []
        for rel in data.get("relations") or []:
            if isinstance(rel, (list, tuple)) and len(rel) == 3:
                relations.append([str(x).strip() for x in rel])
            else:
                raise ValueError(f"{where}的 relations 需要 [src, 关系, dst] 三元组")
        return cls(
            id=str(data.get("id") or f"q{lineno or 0:03d}"),
            question=question,
            kind=kind,
            expected_chunks=_str_list(data.get("expected_chunks")),
            expected_docs=_str_list(data.get("expected_docs")),
            expected_sections=_str_list(data.get("expected_sections")),
            answer_terms=_str_list(data.get("answer_terms")),
            must_not=_str_list(data.get("must_not")),
            relations=relations,
            notes=str(data.get("notes") or ""),
            draft=bool(data.get("draft")),
        )

    @property
    def has_retrieval_gt(self) -> bool:
        return bool(self.expected_chunks or self.expected_docs or self.expected_sections)


def _str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v).strip() for v in value if str(v).strip()]


def load_cases(path: str | Path) -> list[EvalCase]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"评测集不存在：{p}")
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            data = json.loads(line)
            case = EvalCase.from_dict(data, lineno=lineno)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p}:{lineno} 不是合法 JSON：{exc}") from exc
        if case.id in seen:
            raise ValueError(f"{p}:{lineno} 的 id={case.id} 重复")
        seen.add(case.id)
        cases.append(case)
    return cases


def save_cases(path: str | Path, cases: Iterable[EvalCase]) -> int:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(c.to_dict(), ensure_ascii=False) for c in cases]
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


# ---------------------------------------------------------------- 相关性判定


def is_relevant(hit: ChunkHit, case: EvalCase) -> bool:
    """分块是否算"支撑该问题的正确证据"。

    精确 id 命中优先；否则要求文档匹配，再看小节子串。
    """
    if case.expected_chunks and hit.chunk_id in case.expected_chunks:
        return True
    if not case.expected_docs and not case.expected_sections:
        return False
    if case.expected_docs and hit.doc_title not in case.expected_docs:
        return False
    if case.expected_sections:
        heading = hit.heading_path or ""
        return any(section in heading for section in case.expected_sections)
    return True


def _rank_metrics(hits: Sequence[ChunkHit], case: EvalCase, k: int) -> dict[str, Any]:
    relevant_ranks = [
        rank for rank, hit in enumerate(hits[:k], 1) if is_relevant(hit, case)
    ]
    if not case.has_retrieval_gt:
        return {"hit": None, "mrr": None, "ndcg": None, "ranks": []}
    hit = bool(relevant_ranks)
    mrr = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
    dcg = sum(1.0 / math.log2(r + 1) for r in relevant_ranks)
    ideal_n = min(len(relevant_ranks) or 1, k)
    idcg = sum(1.0 / math.log2(r + 1) for r in range(1, ideal_n + 1))
    ndcg = dcg / idcg if idcg else 0.0
    return {"hit": hit, "mrr": mrr, "ndcg": ndcg, "ranks": relevant_ranks}


def _relation_recall(result: RetrievalResult, case: EvalCase) -> float | None:
    if not case.relations:
        return None
    edges = {
        (str(e.get("src_name") or ""), str(e.get("type") or ""), str(e.get("dst_name") or ""))
        for e in result.edges
    }
    found = 0
    for src, rel, dst in case.relations:
        if (src, rel, dst) in edges:
            found += 1
    return found / len(case.relations)


def _term_hit(answer_text: str, terms: Sequence[str]) -> float | None:
    if not terms:
        return None
    text = answer_text or ""
    return sum(1 for t in terms if t in text) / len(terms)


def _abstained(answer: Answer) -> bool:
    if answer.mode == "no-answer":
        return True
    return any(marker in answer.text for marker in ABSTAIN_MARKERS)


# ---------------------------------------------------------------- 单题评测


def evaluate_case(
    case: EvalCase,
    result: RetrievalResult,
    answer: Answer | None,
    *,
    k: int,
    latency_ms: float,
) -> dict[str, Any]:
    ranks = _rank_metrics(result.hits, case, k)
    row: dict[str, Any] = {
        "id": case.id,
        "question": case.question,
        "kind": case.kind,
        "draft": case.draft,
        "hit": ranks["hit"],
        "mrr": ranks["mrr"],
        "ndcg": ranks["ndcg"],
        "ranks": ranks["ranks"],
        "relation_recall": _relation_recall(result, case),
        "latency_ms": round(latency_ms, 1),
        "top": [
            {
                "rank": i,
                "chunk_id": h.chunk_id,
                "doc": h.doc_title,
                "section": h.heading_path,
                "score": round(h.score, 5),
                "relevant": is_relevant(h, case),
            }
            for i, h in enumerate(result.hits[:5], 1)
        ],
        "warnings": list(result.warnings),
    }
    if answer is not None:
        cited_ids = {c.chunk_id for c in answer.citations}
        relevant_ids = {h.chunk_id for h in result.hits[:k] if is_relevant(h, case)}
        cited_relevant = len(cited_ids & relevant_ids)
        row.update(
            {
                "mode": answer.mode,
                "confidence": answer.confidence,
                "abstained": _abstained(answer),
                "term_hit": _term_hit(answer.text, case.answer_terms),
                "violations": [w for w in case.must_not if w in answer.text],
                # 拒答的题没有引用，不该按 0 分计入平均
                "citation_precision": (
                    cited_relevant / len(cited_ids) if cited_ids else None
                ),
                "citation_coverage": (
                    cited_relevant / len(relevant_ids) if relevant_ids else None
                ),
                "answer_excerpt": truncate(answer.text.replace("\n", " "), 220),
                "cited": sorted(cited_ids),
            }
        )
    return row


# ---------------------------------------------------------------- 聚合


def _mean(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def _aggregate(rows: Sequence[dict[str, Any]], k: int) -> dict[str, Any]:
    lat = [r["latency_ms"] for r in rows]
    metrics = {
        "hit@k": _mean(r["hit"] for r in rows),
        "mrr": _mean(r["mrr"] for r in rows),
        "ndcg@k": _mean(r["ndcg"] for r in rows),
        "relation_recall": _mean(r["relation_recall"] for r in rows),
        "term_hit": _mean(r.get("term_hit") for r in rows),
        "citation_precision": _mean(r.get("citation_precision") for r in rows),
        "citation_coverage": _mean(r.get("citation_coverage") for r in rows),
        "abstain_rate": _mean(
            1.0 if r.get("abstained") else 0.0 for r in rows if "abstained" in r
        ),
        "violation_count": sum(len(r.get("violations") or []) for r in rows),
    }
    counts = {
        "cases": len(rows),
        "with_retrieval_gt": sum(1 for r in rows if r["hit"] is not None),
        "with_relations": sum(1 for r in rows if r["relation_recall"] is not None),
        "with_answer": sum(1 for r in rows if "term_hit" in r),
        "with_terms": sum(1 for r in rows if r.get("term_hit") is not None),
    }
    latency = {
        "p50_ms": round(statistics.median(lat), 1) if lat else 0.0,
        "p95_ms": round(sorted(lat)[max(0, int(len(lat) * 0.95) - 1)], 1) if lat else 0.0,
        "max_ms": round(max(lat), 1) if lat else 0.0,
    }
    return {"k": k, "metrics": metrics, "counts": counts, "latency": latency}


def _failure_reasons(row: dict[str, Any], case: EvalCase) -> list[str]:
    reasons: list[str] = []
    if case.kind == "negative":
        if row.get("abstained") is False:
            reasons.append("该拒答却作答")
        return reasons
    if row["hit"] is False:
        reasons.append("检索未命中正确分块")
    if row.get("violations"):
        reasons.append("出现禁止词：" + "、".join(row["violations"]))
    if row.get("term_hit") is not None and row["term_hit"] < 1.0:
        missing = [t for t in case.answer_terms if t not in (row.get("answer_excerpt") or "")]
        if missing:
            reasons.append("答案缺关键信息：" + "、".join(missing))
    if row.get("citation_precision") is not None and row["citation_precision"] < 0.5:
        reasons.append("引用与证据不匹配")
    if row.get("relation_recall") is not None and row["relation_recall"] < 1.0:
        reasons.append("图谱未召回期望关系")
    return reasons


def run_eval(
    conn,
    config: dict,
    provider,
    cases: Sequence[EvalCase],
    *,
    k: int = 8,
    with_answer: bool = True,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    progress = progress or (lambda _m: None)
    retriever = Retriever(conn, config, provider)
    selected = list(cases)[: int(limit)] if limit else list(cases)
    rows: list[dict[str, Any]] = []

    for idx, case in enumerate(selected, 1):
        progress(f"[{idx}/{len(selected)}] {truncate(case.question, 50)}")
        t0 = time.perf_counter()
        result = retriever.search(case.question, k=k)
        answer = None
        if with_answer:
            answer = synthesize(case.question, result, provider, config)
        latency = (time.perf_counter() - t0) * 1000
        row = evaluate_case(case, result, answer, k=k, latency_ms=latency)
        row["reasons"] = _failure_reasons(row, case)
        rows.append(row)

    report = {
        "meta": {
            "generated_at": iso(),
            "k": k,
            "with_answer": with_answer,
            "provider": getattr(provider, "kind", "unknown"),
            "embedding_model": getattr(provider, "embedding_model", "unknown"),
            "db": str(config.get("db_path")),
            "cases": len(rows),
        },
        **_aggregate(rows, k),
        "by_kind": {
            kind: _aggregate([r for r in rows if r["kind"] == kind], k)
            for kind in sorted({r["kind"] for r in rows})
        },
        "failures": [r for r in rows if r["reasons"]],
        "cases": rows,
    }
    report["meta"]["passed"] = len(rows) - len(report["failures"])
    report["meta"]["failed"] = len(report["failures"])
    return report


# 供 ADK 层复用的公共别名（同一套指标口径，避免两份实现漂移）
rank_metrics = _rank_metrics
term_hit = _term_hit
is_abstained = _abstained
failure_reasons = _failure_reasons
aggregate_rows = _aggregate


# ---------------------------------------------------------------- 草稿集生成


RELATION_TEMPLATES: dict[str, tuple[str, str, str]] = {
    # 关系类型 -> (问题模板, 答案取哪个端点, 题目 kind)
    "负责": ("{dst}的负责人是谁？", "src", "relation"),
    "依赖": ("{src}依赖什么？", "dst", "relation"),
    "使用": ("{src}使用了什么？", "dst", "relation"),
    "迁移到": ("{src}迁移到了什么？", "dst", "relation"),
    "替代": ("{src}替代了什么？", "dst", "relation"),
    "属于": ("{src}属于什么？", "dst", "relation"),
    "包含": ("{src}包含什么？", "dst", "relation"),
    "参与": ("{src}参与了什么？", "dst", "relation"),
    "针对": ("{src}针对什么？", "dst", "relation"),
    "产出": ("{src}产出了什么？", "dst", "relation"),
}


def build_draft_cases(
    conn,
    *,
    limit: int = 40,
    min_weight: float = 0.6,
) -> list[EvalCase]:
    """从已抽取的关系生成草稿题：问题是模板化的，证据是关系来源分块。

    这是"半自动标注"：机器给出一份有依据的初稿，人只需要删噪音、补问法。
    比从零手写 30 道题快一个数量级，也比让 LLM 凭空编题可信。
    """
    rows = conn.execute(
        """SELECT r.type_name, r.weight, r.confidence, r.source_chunk_id, r.evidence,
                  s.name AS src, s.type_name AS src_type,
                  d.name AS dst, d.type_name AS dst_type
           FROM relations r
           JOIN entities s ON s.id = r.src_id
           JOIN entities d ON d.id = r.dst_id
           WHERE r.weight >= ? AND r.source_chunk_id IS NOT NULL
           ORDER BY r.weight DESC, r.confidence DESC""",
        (min_weight,),
    ).fetchall()

    cases: list[EvalCase] = []
    seen_questions: set[str] = set()
    for row in rows:
        tpl = RELATION_TEMPLATES.get(row["type_name"])
        if not tpl:
            continue
        question_tpl, answer_side, kind = tpl
        question = question_tpl.format(src=row["src"], dst=row["dst"])
        if question in seen_questions:
            continue
        seen_questions.add(question)
        answer_name = row[answer_side]
        cases.append(
            EvalCase(
                id=f"draft-{len(cases) + 1:03d}",
                question=question,
                kind=kind,
                expected_chunks=[row["source_chunk_id"]],
                answer_terms=[answer_name],
                relations=[[row["src"], row["type_name"], row["dst"]]],
                notes=truncate(row["evidence"] or "", 80),
                draft=True,
            )
        )
        if len(cases) >= limit:
            break
    return cases


# ---------------------------------------------------------------- 校验


def check_cases(conn, cases: Sequence[EvalCase]) -> list[dict[str, str]]:
    """检查标注是否已失效（文档改名、分块重建、小节标题变更）。"""
    docs = {r["title"] for r in conn.execute("SELECT title FROM documents").fetchall()}
    chunk_ids = {r["id"] for r in conn.execute("SELECT id FROM chunks").fetchall()}
    sections = [
        r["heading_path"] or ""
        for r in conn.execute("SELECT heading_path FROM chunks").fetchall()
    ]
    issues: list[dict[str, str]] = []
    for case in cases:
        for doc in case.expected_docs:
            if doc not in docs:
                issues.append({"id": case.id, "field": "expected_docs", "value": doc,
                               "reason": "文档不存在（改名或未摄取）"})
        for cid in case.expected_chunks:
            if cid not in chunk_ids:
                issues.append({"id": case.id, "field": "expected_chunks", "value": cid,
                               "reason": "分块不存在（重建过分块，建议改用 expected_sections）"})
        for section in case.expected_sections:
            if not any(section in s for s in sections):
                issues.append({"id": case.id, "field": "expected_sections", "value": section,
                               "reason": "没有任何分块的小节名包含该字符串"})
        if case.kind != "negative" and not case.has_retrieval_gt:
            issues.append({"id": case.id, "field": "ground_truth", "value": "",
                           "reason": "非 negative 题没有检索标注，检索指标会被跳过"})
    return issues


# ---------------------------------------------------------------- 渲染


def render_text(report: dict[str, Any]) -> str:
    meta, m, c, lat = report["meta"], report["metrics"], report["counts"], report["latency"]

    def fmt(value: float | None, pct: bool = True) -> str:
        if value is None:
            return "  n/a "
        return f"{value * 100:5.1f}%" if pct else f"{value:6.3f}"

    lines = [
        f"评测集 {meta['cases']} 题 · k={meta['k']} · provider={meta['provider']}"
        f" · 答案生成={'开' if meta['with_answer'] else '关'}",
        f"通过 {meta['passed']} / 失败 {meta['failed']}",
        "",
        f"{'指标':<18}{'值':>8}   样本",
        "-" * 40,
        f"{'hit@k':<18}{fmt(m['hit@k']):>8}   {c['with_retrieval_gt']} 题有检索标注",
        f"{'MRR':<18}{fmt(m['mrr'], False):>8}   {c['with_retrieval_gt']} 题",
        f"{'nDCG@k':<18}{fmt(m['ndcg@k'], False):>8}   {c['with_retrieval_gt']} 题",
        f"{'图谱关系召回':<16}{fmt(m['relation_recall']):>8}   {c['with_relations']} 题有期望关系",
        f"{'答案关键词覆盖':<14}{fmt(m['term_hit']):>8}   {c['with_terms']} 题有答案标注",
        f"{'引用准确率':<16}{fmt(m['citation_precision']):>8}   {c['with_answer']} 题",
        f"{'引用覆盖率':<16}{fmt(m['citation_coverage']):>8}   {c['with_answer']} 题",
        f"{'禁止词违规':<16}{m['violation_count']:>8} 处",
        "",
        f"延迟  p50={lat['p50_ms']}ms  p95={lat['p95_ms']}ms  max={lat['max_ms']}ms",
    ]
    if report["by_kind"]:
        lines.append("")
        lines.append("按题型")
        for kind, agg in report["by_kind"].items():
            lines.append(
                f"  {kind:<12} {agg['counts']['cases']:>2} 题  hit={fmt(agg['metrics']['hit@k'])}"
                f"  mrr={fmt(agg['metrics']['mrr'], False)}"
            )
    if report["failures"]:
        lines.append("")
        lines.append(f"失败明细（前 10 / 共 {len(report['failures'])}）")
        for row in report["failures"][:10]:
            lines.append(f"  × [{row['id']}] {row['question']}")
            lines.append(f"    原因：{'；'.join(row['reasons'])}")
            for top in row["top"][:3]:
                mark = "✓" if top["relevant"] else " "
                lines.append(
                    f"    {mark} #{top['rank']} 《{top['doc']}》 › {top['section']}"
                    f"  score={top['score']}"
                )
    return "\n".join(lines)


def render_markdown(report: dict[str, Any]) -> str:
    meta, m, c, lat = report["meta"], report["metrics"], report["counts"], report["latency"]

    def pct(v: float | None) -> str:
        return "n/a" if v is None else f"{v * 100:.1f}%"

    def num(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.3f}"

    out = [
        "# 评测报告",
        "",
        f"- 生成时间：{meta['generated_at']}",
        f"- provider：`{meta['provider']}` / embedding：`{meta['embedding_model']}`",
        f"- 题目数：{meta['cases']}（k={meta['k']}，答案生成={'开' if meta['with_answer'] else '关'}）",
        f"- 通过：{meta['passed']} · 失败：{meta['failed']}",
        "",
        "## 总览",
        "",
        "| 指标 | 值 | 样本 |",
        "| --- | --- | --- |",
        f"| hit@k | {pct(m['hit@k'])} | {c['with_retrieval_gt']} |",
        f"| MRR | {num(m['mrr'])} | {c['with_retrieval_gt']} |",
        f"| nDCG@k | {num(m['ndcg@k'])} | {c['with_retrieval_gt']} |",
        f"| 图谱关系召回 | {pct(m['relation_recall'])} | {c['with_relations']} |",
        f"| 答案关键词覆盖 | {pct(m['term_hit'])} | {c['with_terms']} |",
        f"| 引用准确率 | {pct(m['citation_precision'])} | {c['with_answer']} |",
        f"| 引用覆盖率 | {pct(m['citation_coverage'])} | {c['with_answer']} |",
        f"| 禁止词违规 | {m['violation_count']} 处 | - |",
        f"| 延迟 p50 / p95 | {lat['p50_ms']}ms / {lat['p95_ms']}ms | {len(report['cases'])} |",
    ]
    if report["by_kind"]:
        out += ["", "## 按题型", "", "| 题型 | 题数 | hit@k | MRR | 关系召回 |", "| --- | --- | --- | --- | --- |"]
        for kind, agg in report["by_kind"].items():
            out.append(
                f"| {kind} | {agg['counts']['cases']} | {pct(agg['metrics']['hit@k'])} "
                f"| {num(agg['metrics']['mrr'])} | {pct(agg['metrics']['relation_recall'])} |"
            )
    if report["failures"]:
        out += ["", "## 失败明细", "", "| ID | 问题 | 原因 | Top-3 召回 |", "| --- | --- | --- | --- |"]
        for row in report["failures"]:
            tops = "<br>".join(
                f"{'✓' if t['relevant'] else '×'} #{t['rank']} 《{t['doc']}》›{t['section']}"
                for t in row["top"][:3]
            )
            out.append(
                f"| {row['id']} | {row['question']} | {'；'.join(row['reasons'])} | {tops} |"
            )
    return "\n".join(out)


# ---------------------------------------------------------------- 对比


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """只在两次运行共有的题目上比指标，避免"少跑几题"看起来像变好。"""
    base_rows = {c["id"]: c for c in baseline.get("cases") or []}
    cand_rows = {c["id"]: c for c in candidate.get("cases") or []}
    shared = sorted(set(base_rows) & set(cand_rows))

    # 报告里的指标名 → 逐题行里的字段名
    keys = [
        ("hit@k", "hit"),
        ("mrr", "mrr"),
        ("ndcg@k", "ndcg"),
        ("relation_recall", "relation_recall"),
        ("term_hit", "term_hit"),
        ("citation_precision", "citation_precision"),
        ("citation_coverage", "citation_coverage"),
    ]
    rows = []
    for key, field_name in keys:
        if shared:
            before = _mean(base_rows[i].get(field_name) for i in shared)
            after = _mean(cand_rows[i].get(field_name) for i in shared)
        else:  # 报告里没有逐题数据时退回总分
            before = baseline["metrics"].get(key)
            after = candidate["metrics"].get(key)
        delta = None if (before is None or after is None) else round(after - before, 4)
        rows.append({"metric": key, "baseline": before, "candidate": after, "delta": delta})

    if shared:
        base_lat = _mean(base_rows[i].get("latency_ms") for i in shared)
        cand_lat = _mean(cand_rows[i].get("latency_ms") for i in shared)
    else:
        base_lat = baseline["latency"]["p50_ms"]
        cand_lat = candidate["latency"]["p50_ms"]
    latency_delta = None
    if base_lat is not None and cand_lat is not None:
        latency_delta = round(cand_lat - base_lat, 1)

    return {
        "baseline": baseline["meta"],
        "candidate": candidate["meta"],
        "rows": rows,
        "latency_delta_ms": latency_delta,
        "scope": {
            "shared_cases": len(shared),
            "baseline_cases": len(base_rows),
            "candidate_cases": len(cand_rows),
        },
        "warnings": [
            w for w in [
                "两次运行的 k 不同，排序类指标不可直接比较"
                if baseline["meta"].get("k") != candidate["meta"].get("k") else "",
                f"只有 {len(shared)} 题是共同题目，指标按共同题目计算"
                if shared and len(shared) < max(len(base_rows), len(cand_rows)) else "",
            ] if w
        ],
    }
