"""答案生成：把召回到的证据拼成带引用的回答。

两条路径：
  * 真实模型：严格"只依据证据"，要求 [n] 引用，事后校验引用编号是否越界；
  * 离线 mock：抽取式摘要（挑与问题词重叠最高的句子），明确标注为抽取结果，
    避免在没有人写答案的情况下伪造流畅的幻觉。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from .retrieval import ChunkHit, RetrievalResult
from .textutil import FUNCTION_CHARS, embed_tokens, split_sentences
from .util import truncate

CITE_RE = re.compile(r"\[(\d+(?:\s*[,，]\s*\d+)*)\]")

SYSTEM_PROMPT = """你是知识库问答助手，回答必须完全基于给定的“证据”区块。

规则：
1. 只使用证据中的信息，不补充外部知识；证据不足时直接说明缺什么。
2. 每个关键结论后用方括号编号标注来源，例如 [1] 或 [2,3]。
3. 编号只能来自证据区块，严禁编造。
4. 如果多个证据相互冲突，指出冲突并分别标注来源。
5. 用中文回答，先给结论，再给依据；能用一两句说清就不要展开。"""


@dataclass
class Citation:
    index: int
    chunk_id: str
    doc_id: str
    doc_title: str
    heading_path: str
    snippet: str

    def label(self) -> str:
        path = self.heading_path or ""
        # 标题路径通常以文档标题开头，避免"《X》 › X › Y"的重复
        if path.startswith(self.doc_title):
            path = path[len(self.doc_title) :].lstrip(" ›")
        return f"《{self.doc_title}》{' › ' + path if path else ''}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "doc_title": self.doc_title,
            "heading_path": self.heading_path,
            "snippet": self.snippet,
            "label": self.label(),
        }


@dataclass
class Answer:
    text: str
    citations: list[Citation] = field(default_factory=list)
    mode: str = "extractive"  # llm | extractive | no-answer
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.text,
            "mode": self.mode,
            "confidence": self.confidence,
            "citations": [c.to_dict() for c in self.citations],
            "warnings": self.warnings,
        }


def _evidence_blocks(hits: Sequence[ChunkHit]) -> tuple[str, list[Citation]]:
    blocks: list[str] = []
    citations: list[Citation] = []
    for i, hit in enumerate(hits, 1):
        cit = Citation(
            index=i,
            chunk_id=hit.chunk_id,
            doc_id=hit.doc_id,
            doc_title=hit.doc_title,
            heading_path=hit.heading_path,
            snippet=truncate(hit.text, 220),
        )
        citations.append(cit)
        blocks.append(f"[{i}] {cit.label()}\n{truncate(hit.text, 1200)}")
    return "\n\n".join(blocks), citations


def _facts_block(
    result: RetrievalResult,
    limit: int = 10,
    seed_only: bool = False,
    question: str = "",
) -> str:
    """优先展示与问题实体直接相连的边，并按问题里的关系词过滤。"""
    seeds = set(result.seeds or ())
    edges = result.edges
    if seed_only and seeds:
        edges = [e for e in edges if e.get("src") in seeds or e.get("dst") in seeds]
    wanted = {e["type"] for e in edges if e.get("type") and question and e["type"] in question}
    ordered = sorted(
        edges,
        key=lambda e: (
            0 if (e.get("src") in seeds or e.get("dst") in seeds) else 1,
            0 if e.get("type") in wanted else 1,  # 问题里提到的关系排前面，但不丢其它边
            -float(e.get("weight", 0)),
        ),
    )
    lines: list[str] = []
    for edge in ordered[:limit]:
        evidence = f"（证据：{truncate(edge['evidence'], 40)}）" if edge.get("evidence") else ""
        lines.append(f"- {edge['src_name']} --{edge['type']}--> {edge['dst_name']}{evidence}")
    return "\n".join(lines)


def _memory_block(result: RetrievalResult) -> str:
    lines: list[str] = []
    for mem in result.memories:
        if mem["score"] <= 0.02:
            continue
        lines.append(f"- [{mem['kind']}] {mem['content']}")
    return "\n".join(lines)


def build_prompt(question: str, result: RetrievalResult) -> tuple[str, list[Citation]]:
    blocks, citations = _evidence_blocks(result.hits)
    parts = [f"问题：{question}", f"证据：\n{blocks}"]
    facts = _facts_block(result)
    if facts:
        parts.append(f"图谱事实（可辅助推理，引用时仍需对应到上面的编号）：\n{facts}")
    memories = _memory_block(result)
    if memories:
        parts.append(f"关于使用者的长期记忆（不是文档证据，回答时不要编号引用）：\n{memories}")
    parts.append("请作答。")
    return "\n\n".join(parts), citations


def should_abstain(question: str, result: RetrievalResult, cfg: dict) -> bool:
    """问题里出现语料从未有过的实词时，拒答而不是硬凑。

    这是防"问 Kubernetes，答林晓"的闸门：向量/关键词检索总会返回"看起来相关"
    的分块，如果问题中的实词（不含疑问词、虚词）一个都没在语料里出现过，
    那么唯一安全的动作是承认不知道。
    """
    if not cfg.get("abstain_on_unknown_terms", True):
        return False
    if not result.hits:
        return True
    unknown = list(result.unknown_terms or [])
    if not unknown:
        return False
    # 语义层已经认出了问题里的核心对象（term 命中）时，剩下的未知字多半是
    # 动词或说法差异（"换/组/件"），不足以判超纲；只有陌生的英文专名才继续拦。
    if getattr(result, "term_hits", None) and not any(
        u.isascii() and len(u) >= 3 for u in unknown
    ):
        return False
    # 单个生僻字可能是说法差异；成串的未知实字，或一个陌生的英文专名，才判为超纲
    if any(u.isascii() and len(u) >= 3 for u in unknown):
        return True
    return len(unknown) >= int(cfg.get("abstain_min_unknown", 2))


def synthesize(
    question: str,
    result: RetrievalResult,
    provider,
    config: dict,
) -> Answer:
    cfg = config.get("answer", {}) or {}
    min_score = float(cfg.get("min_score", 0.005))
    hits = [h for h in result.hits if h.score >= min_score][: int(cfg.get("max_hits", 8))]
    if not hits:
        return Answer(
            text="知识库里没有检索到与这个问题相关的内容。可以换个说法，或先摄取相关文档。",
            mode="no-answer",
            warnings=result.warnings,
        )
    if should_abstain(question, result, cfg):
        return Answer(
            text="知识库里没有找到能回答这个问题的证据（问题中的关键对象不在已有知识里）。"
            "可以先摄取相关文档，或换一种指代已知实体的问法。",
            mode="no-answer",
            warnings=result.warnings,
        )
    if provider is None or provider.is_mock:
        return _extractive(question, hits, result, cfg)

    prompt, citations = build_prompt(question, result)
    try:
        text = provider.chat.complete(
            SYSTEM_PROMPT, prompt, max_tokens=int(cfg.get("max_tokens", 900))
        )
    except Exception as exc:  # noqa: BLE001 - 模型失败降级为抽取式
        fallback = _extractive(question, hits, result, cfg)
        fallback.warnings.append(f"LLM 调用失败，已降级为抽取式摘要：{type(exc).__name__}: {exc}")
        return fallback
    if not text:
        return _extractive(question, hits, result, cfg)

    used = _cited_indices(text, citations)
    answer = Answer(
        text=text.strip(),
        citations=[c for c in citations if c.index in used] or citations[:1],
        mode="llm",
        confidence=_confidence(hits, used),
        warnings=list(result.warnings),
    )
    if not used:
        answer.warnings.append("模型回答中没有检测到有效引用编号，请自行核对来源")
    return answer


def _cited_indices(text: str, citations: Sequence[Citation]) -> set[int]:
    valid = {c.index for c in citations}
    used: set[int] = set()
    for m in CITE_RE.finditer(text):
        for part in re.split(r"[,，]", m.group(1)):
            part = part.strip()
            if part.isdigit() and int(part) in valid:
                used.add(int(part))
    return used


def _confidence(hits: Sequence[ChunkHit], used: set[int]) -> float:
    if not hits:
        return 0.0
    base = min(0.55, hits[0].score * 3.5)
    bonus = min(0.25, 0.08 * len(used))
    agreement = 0.08 if len(hits) > 2 else 0.0
    return round(min(0.95, 0.15 + base + bonus + agreement), 2)


def compose_extractive(
    question: str,
    hits: Sequence[ChunkHit],
    result: RetrievalResult,
    cfg: dict | None = None,
) -> Answer:
    """供 ADK 层的离线模型复用：给定检索结果，产出带引用的抽取式答案。"""
    return _extractive(question, hits, result, cfg)


def _chunk_body(hit: ChunkHit) -> str:
    """去掉分块正文前缀里的标题路径，避免把标题当成一句话。"""
    body = hit.text
    if hit.heading_path and body.startswith(hit.heading_path):
        body = body[len(hit.heading_path) :].lstrip("\n")
    return body


QUESTION_NOISE = {
    "我们", "你们", "他们", "什么", "为什", "什么", "怎么", "怎样", "如何",
    "哪些", "哪个", "哪里", "是否", "可以", "需要", "这个", "那个", "以及",
    "还是", "多少", "几个", "一下", "现在", "目前",
}


def _extractive(
    question: str,
    hits: Sequence[ChunkHit],
    result: RetrievalResult,
    cfg: dict | None = None,
) -> Answer:
    """离线模式：优先用图谱证据句，再用"稀有词覆盖"挑原文句子。

    关键点：句子打分不能只看总重叠，否则问题里的实体名会把整条相似度吃掉，
    真正承载答案的稀有词（"启动时间""Session"）反而选不上。
    """
    cfg = cfg or {}
    q_units = [t for t, _w in embed_tokens(question)]
    q_set = set(q_units)

    all_sents: list[tuple[int, str, set[str]]] = []
    for idx, hit in enumerate(hits[:6], 1):
        for sent in split_sentences(_chunk_body(hit)):
            clean = sent.strip().lstrip("-*+ ").strip()
            if len(clean) < 5:
                continue
            toks = {t for t, _w in embed_tokens(clean)}
            all_sents.append((idx, clean, toks))
    if not all_sents:
        return Answer(
            text="检索到相关片段，但没有可用的句子。",
            citations=[], mode="no-answer", warnings=list(result.warnings),
        )

    total = len(all_sents)
    df: dict[str, int] = {}
    for _idx, _sent, toks in all_sents:
        for tok in toks:
            df[tok] = df.get(tok, 0) + 1

    def idf(tok: str) -> float:
        return math.log(1.0 + total / max(1, df.get(tok, 0)))

    # 只用手头确实出现过的查询词打分；"从未出现过"的词不影响排序
    present = [(t, idf(t)) for t in dict.fromkeys(q_units) if df.get(t, 0) > 0]
    if not present:
        return Answer(
            text="检索到相关片段，但没有能与问题直接对应的句子。可以换一种问法。",
            citations=[], mode="no-answer", warnings=list(result.warnings),
        )
    present.sort(key=lambda x: -x[1])
    # 稀有词（IDF 前 40%）才是区分度所在，用它们做主要依据
    focus_n = max(1, int(len(present) * 0.4))
    focus = {t for t, _w in present[:focus_n]}
    focus_idf_sum = sum(w for t, w in present[:focus_n]) or 1.0
    all_idf_sum = sum(w for _t, w in present) or 1.0

    # 问题点了名的实体，答案句大概率就在提到它的句子里；这是图谱给检索的白送信息
    seed_names = [
        e["name"] for e in result.entities
        if e.get("id") in set(result.seeds or ()) and e.get("name")
    ]

    def score_of(toks: set[str], sent: str) -> float:
        rare = sum(idf(t) for t in focus & toks)
        if rare <= 0:
            return 0.0
        broad = sum(idf(t) for t in toks & q_set)
        base = (rare / focus_idf_sum) * (0.6 + 0.4 * broad / all_idf_sum)
        return base * (1.6 if any(n in sent for n in seed_names) else 1.0)

    scored = [(score_of(toks, sent), idx, sent) for idx, sent, toks in all_sents]
    scored = [x for x in scored if x[0] > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))

    # 只保留"覆盖了问题实义词（≥2 字）"的句子；单字重叠（"我""们"）不足以
    # 证明这句在回答本题——ADR-001 的"我们决定…"就是这样挤掉答案句的。
    focus_bigrams = {
        t for t, _w in embed_tokens(question)
        if len(t) >= 2 and t not in QUESTION_NOISE and not (set(t) & FUNCTION_CHARS)
    }
    if focus_bigrams:
        strong = [x for x in scored if focus_bigrams & {t for t, _ in embed_tokens(x[2])}]
        if strong:
            scored = strong

    # 图谱证据句优先（关系原话通常就是答案）
    seeds = set(result.seeds or ())
    picks: list[tuple[int, str]] = []
    seen_ev: set[str] = set()
    for edge in result.edges:
        if not (edge.get("src") in seeds or edge.get("dst") in seeds):
            continue
        # 关系词必须出现在问句里，否则"某人的负责人是谁"会把无关的负责边当成答案
        if edge["type"] not in question:
            continue
        evidence = (edge.get("evidence") or "").strip()
        if not evidence:
            continue
        # 证据可能带 "- " 列表前缀，候选句已去掉，所以两种写法都要试
        keys = [evidence.lstrip("-*+ \t").strip()[:12], evidence[:12]]
        for idx, sent, _toks in all_sents:
            if sent in seen_ev:
                continue
            if any(k and k in sent for k in keys):
                seen_ev.add(sent)
                picks.append((idx, sent))
                break
        if len(picks) >= 2:
            break

    floor = scored[0][0] * float(cfg.get("extractive_floor_ratio", 0.55))
    for score, idx, sent in scored:
        if len(picks) >= 2:  # 抽取式只回答"最相关的一两句"，宁短勿杂
            break
        if score < floor:
            continue
        picks.append((idx, sent))

    # 问"为什么"时，答案句往往是"原因/因为/由于"那一句，而不是相关性最高那句。
    # 在同一分块里补一句因果句，避免答了结论却没说原因。
    if picks and len(picks) < 2 and any(k in question for k in ("为什么", "为何", "原因", "缘何")):
        want_idx = picks[0][0]
        for idx, sent, _toks in all_sents:
            if idx != want_idx:
                continue
            if any(k in sent for k in ("原因", "因为", "由于", "之所以")):
                picks.append((idx, sent))
                break

    chosen: list[tuple[int, str]] = []
    seen: set[str] = set()
    for idx, sent in picks:
        key = sent[:20]
        if key in seen:
            continue
        seen.add(key)
        chosen.append((idx, sent))
        if len(chosen) >= 2:
            break

    citations = [
        Citation(
            index=idx, chunk_id=hit.chunk_id, doc_id=hit.doc_id,
            doc_title=hit.doc_title, heading_path=hit.heading_path,
            snippet=truncate(hit.text, 220),
        )
        for idx, hit in enumerate(hits, 1)
    ]
    if not chosen:
        return Answer(
            text="检索到相关片段，但没有能与问题直接对应的句子。可以扩大召回范围（--k）或换一种问法。",
            citations=citations[:3], mode="no-answer",
            warnings=list(result.warnings),
        )
    lines = [f"（离线抽取式摘要，未调用大模型）与「{question}」最相关的原文：", ""]
    for idx, sent in chosen:
        lines.append(f"- {truncate(sent, 260)} [{idx}]")
    used = {idx for idx, _ in chosen}
    q_lex = {t for t, _ in embed_tokens(question)}
    mems = []
    for m in result.memories:
        shared = q_lex & {t for t, _ in embed_tokens(m["content"])}
        # 单个单字重合（"会"）不算相关，要求共享一个二元组或至少两个 token
        if m["score"] >= 0.3 and (any(len(t) >= 2 for t in shared) or len(shared) >= 2):
            mems.append(m)
    mems = mems[:3]
    if mems:
        lines.extend(["", "相关长期记忆："] + [f"- {m['content']}" for m in mems])
    facts = _facts_block(result, seed_only=True, limit=6, question=question)
    if facts:
        lines.extend(["", "图谱中的直接关系：", facts])
    return Answer(
        text="\n".join(lines).strip(),
        citations=[c for c in citations if c.index in used],
        mode="extractive",
        confidence=_confidence(hits, used),
        warnings=list(result.warnings),
    )
