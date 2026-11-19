"""文本处理：规范化、分句、Markdown 感知分块、无模型特征向量 token。

分块策略对本项目很关键：
  * 按标题层级保留 heading path，让引用的上下文完整（"北极星项目 › 架构决策"）；
  * 段落优先、超长段落再按句子切，绝不切在句子中间；
  * 相邻块保留少量重叠，降低"答案被切断"的概率。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
CJK_CLASS = f"[{CJK}]"
CJK_RE = re.compile(CJK_CLASS)
TOKEN_RE = re.compile(f"[a-z0-9_]+|[A-Za-z0-9_]+|[{CJK}]+")
SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*|\n+")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
STOPWORDS_EN = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "with", "as", "by", "at", "from", "that", "this", "it",
}
STOPWORDS_ZH = set("的了是在和有我你他她它们与及或就都也很不一个上下来去对为以")
PUNCT_RE = re.compile(r"[\s\u3000!-/:-@\[-`{-~·—–'\"“”‘’（）《》〈〉【】、，。；：？！…]+")


def has_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


# 中文标点在全角块里，但不该被转成 ASCII（否则正文面目全非）
_KEEP_FULLWIDTH = {0xFF01, 0xFF08, 0xFF09, 0xFF0C, 0xFF1A, 0xFF1B, 0xFF1F}


def normalize_text(text: str) -> str:
    """NFC + 折叠空白；全角字母数字转半角，但保留中文标点。"""
    text = unicodedata.normalize("NFC", text or "")
    buf: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E and code not in _KEEP_FULLWIDTH:
            buf.append(chr(code - 0xFEE0))
        elif code == 0x3000:  # 全角空格
            buf.append(" ")
        elif code in (0x200B, 0xFEFF):
            continue
        else:
            buf.append(ch)
    out = "".join(buf)
    out = re.sub(r"[ \t]+", " ", out)
    return out.strip()


def normalize_name(name: str) -> str:
    """实体名归一化：只保留字母/数字/汉字，小写。用于别名匹配。"""
    t = unicodedata.normalize("NFKC", name or "").lower()
    return re.sub(rf"[^0-9a-z{CJK}]+", "", t)


def soft_key(text: str) -> str:
    """用于记忆去重的松散键：去标点、去虚词。"""
    t = normalize_text(text).lower()
    t = PUNCT_RE.sub("", t)
    return "".join(ch for ch in t if ch not in STOPWORDS_ZH)


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in SENT_SPLIT_RE.split(text or "")]
    return [p for p in parts if p]


def embed_tokens(text: str, *, bigram: bool = True) -> list[tuple[str, float]]:
    """把文本拆成 (token, 权重)，供特征哈希向量使用。

    中文用单字 + 二元组（不引入分词器，跨领域稳定）；英文/数字用词。
    """
    out: list[tuple[str, float]] = []
    for m in TOKEN_RE.finditer(normalize_text(text).lower()):
        tok = m.group()
        if CJK_RE.match(tok):
            for ch in tok:
                out.append((ch, 0.45))
            if bigram:
                for i in range(len(tok) - 1):
                    out.append((tok[i : i + 2], 1.0))
        else:
            if tok in STOPWORDS_EN or len(tok) == 1:
                continue
            out.append((tok, 1.0))
    return out


QUESTION_WORDS = {
    "什么", "怎么", "怎样", "如何", "为什么", "为啥", "哪个", "哪些", "哪里",
    "谁", "多少", "几个", "是否", "是不是", "有没有", "请问", "一下", "分别",
}


# 疑问词与高频虚字：判断 OOV 时要排除，否则"为什么"会被当成未知实词
FUNCTION_CHARS = set(
    "的了是在和有我你他她它们与及或就都也很不一个上下来去对为以什么怎怎样哪谁多少"
    "如何是否请问几这那就还并而但把被将给让向从到于其之着过又更最很太"
)


def content_units(text: str) -> list[str]:
    """问题里的"实"单位：汉字逐字 + 英文词。

    中文不做分词，所以用字级判断；二元组会跨越词边界造出"布窗"这种假词。
    """
    out: list[str] = []
    for m in TOKEN_RE.finditer(normalize_text(text).lower()):
        tok = m.group()
        if CJK_RE.match(tok):
            out.extend(ch for ch in tok if ch not in FUNCTION_CHARS)
        elif len(tok) >= 2 and tok not in STOPWORDS_EN:
            out.append(tok)
    return list(dict.fromkeys(out))


def keyword_terms(text: str, *, min_len: int = 2, limit: int = 24) -> list[str]:
    """给 FTS 用的检索词：英文词 + 中文二元组，去重保序。"""
    seen: set[str] = set()
    out: list[str] = []
    for m in TOKEN_RE.finditer(normalize_text(text).lower()):
        tok = m.group()
        if CJK_RE.match(tok):
            cands = [tok[i : i + 2] for i in range(len(tok) - 1)] or [tok]
        else:
            cands = [tok] if len(tok) >= min_len and tok not in STOPWORDS_EN else []
        for c in cands:
            if len(c) < min_len or c in seen:
                continue
            seen.add(c)
            out.append(c)
            if len(out) >= limit:
                return out
    return out


# ---------------------------------------------------------------- 分块


def _line_offsets(lines: list[str]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    return offsets


def _units(text: str, max_chars: int) -> list[str]:
    """把超长块拆成"行优先、超长行再按句拆"的单元，保留行结构。"""
    units: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        is_bullet = line.lstrip().startswith(("- ", "* ", "+ ")) or re.match(
            r"^\s*\d+[.)]\s", line
        )
        if is_bullet or len(line) <= max_chars:
            units.append(line + "\n")
            continue
        for sent in split_sentences(line):
            units.append(sent + "\n")
    return units or [text]


def _split_long_block(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长段落：按行/句打包，块间做尾部重叠。"""
    pieces: list[str] = []
    cur = ""
    for unit in _units(text, max_chars):
        if len(unit) > max_chars:  # 极端情况：单句超长，硬切
            if cur.strip():
                pieces.append(cur)
            cur = ""
            for i in range(0, len(unit), max(1, max_chars - overlap)):
                pieces.append(unit[i : i + max_chars])
            continue
        if cur and len(cur) + len(unit) > max_chars:
            pieces.append(cur)
            cur = _overlap_tail(cur, overlap) + unit
        else:
            cur += unit
    if cur.strip():
        pieces.append(cur)
    return pieces


def _overlap_tail(text: str, overlap: int) -> str:
    if overlap <= 0 or not text:
        return ""
    tail = text[-overlap:]
    # 尽量从句子边界开始，避免半句重复
    for i, ch in enumerate(tail):
        if ch in "。！？!?；;\n":
            return tail[i + 1 :]
    return tail


def chunk_markdown(
    text: str,
    *,
    target_chars: int = 700,
    max_chars: int = 1200,
    overlap_chars: int = 120,
) -> list[dict]:
    """把 Markdown（或纯文本）切成带标题路径的块。

    返回：[{ord, text, heading_path, char_start, char_end}]
    """
    text = normalize_text(text)
    lines = text.splitlines()
    offsets = _line_offsets(lines)

    blocks: list[dict] = []
    stack: list[str] = []
    buf: list[str] = []
    buf_start_line = 0
    buf_start_char = 0

    def flush() -> None:
        nonlocal buf
        body = "\n".join(buf).strip()
        if body:
            blocks.append(
                {
                    "heading": " › ".join(stack),
                    "text": body,
                    "start": buf_start_char,
                    "end": buf_start_char + len("\n".join(buf)),
                }
            )
        buf = []

    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            flush()
            level = len(m.group(1))
            stack = stack[: max(0, level - 1)] + [m.group(2).strip()]
            buf_start_line = idx
            buf_start_char = offsets[idx]
            continue
        if not line.strip():
            flush()
            buf_start_line = idx + 1
            buf_start_char = offsets[idx + 1] if idx + 1 < len(offsets) else len(text)
            continue
        if not buf:
            buf_start_char = offsets[idx]
        buf.append(line)
    flush()

    if not blocks:
        blocks = [{"heading": "", "text": text, "start": 0, "end": len(text)}]

    chunks: list[dict] = []
    cur_parts: list[dict] = []
    cur_len = 0

    def close() -> None:
        nonlocal cur_parts, cur_len
        if not cur_parts:
            return
        # 以块内首个小节作为引用路径；跨小节的块在打包时已被拆开
        heading_path = cur_parts[0]["heading"] or ""
        body = "\n\n".join(p["text"] for p in cur_parts)
        # 只有真正超长才重排，否则原样保留段内换行（列表项、代码块等结构）
        pieces = (
            [body]
            if len(body) <= max_chars
            else _split_long_block(body, max_chars, 0)  # 重叠统一在下面的后处理里加
        )
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            # 标题路径拼进正文：既提升 FTS 命中，也让引用自带上下文
            full = f"{heading_path}\n{piece}" if heading_path else piece
            chunks.append(
                {
                    "ord": len(chunks),
                    "text": full,
                    "heading_path": heading_path,
                    "char_start": cur_parts[0]["start"],
                    "char_end": cur_parts[-1]["end"],
                }
            )
        cur_parts = []
        cur_len = 0

    for blk in blocks:
        blen = len(blk["text"])
        # 超出目标长度，或发生了小节切换 → 收尾当前块（保证引用路径准确）
        if cur_parts and (cur_len + blen > target_chars or blk["heading"] != cur_parts[-1]["heading"]):
            close()
        cur_parts.append(blk)
        cur_len += blen + 2
    close()

    # 相邻块补齐重叠（跨段落边界的场景）
    if overlap_chars > 0:
        for i in range(1, len(chunks)):
            # 跨小节不加重叠：否则引用会指到"看起来属于上一节"的内容
            if chunks[i]["heading_path"] != chunks[i - 1]["heading_path"]:
                continue
            prev = chunks[i - 1]["text"]
            tail = _overlap_tail(prev, min(overlap_chars, target_chars // 4))
            room = max_chars - len(chunks[i]["text"]) - 1
            if room > 0:
                tail = tail[-room:]
            if tail and not chunks[i]["text"].startswith(tail):
                head = chunks[i]["text"]
                # 保留标题行在前，重叠内容插在其后
                if "\n" in head:
                    first, rest = head.split("\n", 1)
                    chunks[i]["text"] = f"{first}\n{tail}\n{rest}"
                else:
                    chunks[i]["text"] = f"{tail}\n{head}"
    return chunks


def extract_heading_path(text: str) -> str:
    for line in text.splitlines():
        m = HEADING_RE.match(line)
        if m:
            return m.group(2).strip()
    return ""


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
