# -*- coding: utf-8 -*-
"""文件解析层：把「任意支持的文件」变成「带标题结构的纯文本」。

只负责文件 → 文本这一步，不碰分块、向量、图谱与检索：
    parse_file(path) -> ParsedDoc(title, text, doc_type)

这样 PDF / DOCX 接进来时，`Ingest` 的后续流程（chunk_markdown / FTS / 向量 / 抽取）
一行都不用改。缺依赖时抛 ParserUnavailable，提示装 `kb[parse]`。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".text", ".rst"}


class ParserUnavailable(RuntimeError):
    """缺少解析某个格式所需的可选依赖。"""


@dataclass
class ParsedDoc:
    title: str
    text: str
    doc_type: str = "note"


# ---------------------------------------------------------------- 文本

def _title_from_text(path: Path, text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip() or path.stem
        if s:
            return s[:60]
    return path.stem


def parse_text(path: Path) -> ParsedDoc:
    text = path.read_text(encoding="utf-8", errors="replace")
    return ParsedDoc(title=_title_from_text(path, text), text=text, doc_type="note")


# ---------------------------------------------------------------- PDF

_PAGE_MARK = re.compile(r"^(?:[-—\s]*\d+[-—\s]*|第\s*\d+\s*页|page\s*\d+)$", re.I)


def _clean_pdf_text(pages: list[str]) -> str:
    """去掉页码/重复页眉页脚，修好连字符断行，压掉多余空行。"""
    counts: Counter[str] = Counter()
    for page in pages:
        for line in {x.strip() for x in page.splitlines() if x.strip()}:
            counts[line] += 1
    # 页眉页脚的特征：在大多数页出现、很短，且带页码或纯数字
    repeated = {
        line for line, n in counts.items()
        if n >= 3 and len(line) < 60 and (any(ch.isdigit() for ch in line) or _PAGE_MARK.match(line))
    }

    cleaned = []
    for page in pages:
        lines = []
        for line in page.splitlines():
            line = re.sub(r"[ \t]{2,}", " ", line).strip()   # PDF 常用空格排版，先压掉
            if not line or line in repeated or _PAGE_MARK.match(line):
                continue
            lines.append(line)
        cleaned.append("\n".join(lines))
    text = "\n\n".join(cleaned)
    text = re.sub(r"-\n(?=[A-Za-z])", "", text)     # 英文断词
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_pdf(path: Path) -> ParsedDoc:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ParserUnavailable(
            "解析 PDF 需要 pypdf：pip install 'kb[parse]'（或 pip install pypdf）"
        ) from exc

    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    text = _clean_pdf_text(pages)
    if not text:
        raise ValueError(f"{path.name}: 没有抽取到文本（可能是扫描件，需要 OCR）")

    meta_title = ""
    try:
        meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
    except Exception:  # noqa: BLE001 - 元数据坏了不影响正文
        meta_title = ""
    return ParsedDoc(title=meta_title or path.stem, text=text, doc_type="pdf")


# ---------------------------------------------------------------- DOCX

def parse_docx(path: Path) -> ParsedDoc:
    try:
        import docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ParserUnavailable(
            "解析 DOCX 需要 python-docx：pip install 'kb[parse]'（或 pip install python-docx）"
        ) from exc

    doc = docx.Document(str(path))
    blocks: list[str] = []

    def iter_blocks():
        for child in doc.element.body.iterchildren():
            if child.tag == qn("w:p"):
                yield Paragraph(child, doc)
            elif child.tag == qn("w:tbl"):
                yield Table(child, doc)

    for block in iter_blocks():
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            style = (block.style.name or "").lower()
            if style.startswith("heading"):
                digits = "".join(ch for ch in style if ch.isdigit())
                level = max(1, min(6, int(digits or 1)))
                blocks.append("#" * level + " " + text)
            else:
                blocks.append(text)
        else:                                   # 表格 → Markdown 表格，避免被拆成散句
            rows = [[cell.text.strip().replace("\n", " ") for cell in row.cells] for row in block.rows]
            if not rows:
                continue
            sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
            blocks.append("\n".join(["| " + " | ".join(rows[0]) + " |", sep]
                                    + ["| " + " | ".join(r) + " |" for r in rows[1:]]))

    text = "\n\n".join(blocks).strip()
    if not text:
        raise ValueError(f"{path.name}: 文档没有可提取的文本")

    title = ""
    try:
        title = (doc.core_properties.title or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    return ParsedDoc(title=title or path.stem, text=text, doc_type="doc")


# ---------------------------------------------------------------- 注册表

_REGISTRY: dict[str, Callable[[Path], ParsedDoc]] = {}


def register(suffixes: set[str], parser: Callable[[Path], ParsedDoc]) -> None:
    for suffix in suffixes:
        _REGISTRY[suffix.lower()] = parser


def supported_suffixes() -> set[str]:
    return set(_REGISTRY)


def parse_file(path: Path) -> ParsedDoc:
    path = Path(path)
    parser = _REGISTRY.get(path.suffix.lower())
    if parser is None:
        raise ParserUnavailable(
            f"不支持的格式：{path.suffix or '(无后缀)'}（支持 {', '.join(sorted(_REGISTRY))}）"
        )
    doc = parser(path)
    if not (doc.text or "").strip():
        raise ValueError(f"{path.name}: 解析结果为空")
    return doc


register(TEXT_SUFFIXES, parse_text)
register({".pdf"}, parse_pdf)
register({".docx"}, parse_docx)
