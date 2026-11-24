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




# ---------------------------------------------------------------- PPTX

def _table_markdown(rows: list[list[str]]) -> str:
    rows = [[str(cell or "").replace("\n", " ").strip() for cell in row] for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    head = "| " + " | ".join(rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    return "\n".join([head, sep] + ["| " + " | ".join(row) + " |" for row in rows[1:]])


def _pptx_shape_text(shape, out: list[str]) -> None:
    if getattr(shape, "has_table", False):
        rows = [[cell.text for cell in row.cells] for row in shape.table.rows]
        table = _table_markdown(rows)
        if table:
            out.append(table)
        return
    if getattr(shape, "has_text_frame", False):
        for paragraph in shape.text_frame.paragraphs:
            line = (paragraph.text or "").strip()
            if line:
                out.append(line)
    if hasattr(shape, "shapes"):
        for child in shape.shapes:
            _pptx_shape_text(child, out)


def parse_pptx(path: Path) -> ParsedDoc:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ParserUnavailable(
            "解析 PPTX 需要 python-pptx：pip install python-pptx"
        ) from exc

    prs = Presentation(str(path))
    blocks = [f"# {path.stem}"]
    for index, slide in enumerate(prs.slides, 1):
        if index > 500:
            blocks.append(f"## 第 {index} 页以后已截断")
            break
        lines: list[str] = []
        for shape in slide.shapes:
            _pptx_shape_text(shape, lines)
        try:
            notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:  # noqa: BLE001
            notes = ""
        if notes:
            lines.append("备注：" + notes.replace("\n", " "))
        if lines:
            blocks.append(f"## 第 {index} 页\n" + "\n".join(lines))
    text = "\n\n".join(blocks).strip()
    if len(blocks) == 1:
        raise ValueError(f"{path.name}: 演示文稿没有可提取的文本")
    title = ""
    try:
        title = (prs.core_properties.title or "").strip()
    except Exception:  # noqa: BLE001
        title = ""
    return ParsedDoc(title=title or path.stem, text=text, doc_type="slides")


# ---------------------------------------------------------------- Excel

MAX_SHEET_ROWS = 2000
MAX_SHEET_COLS = 60


def _sheet_markdown(rows: list[list[str]]) -> str:
    trimmed = []
    for row in rows[:MAX_SHEET_ROWS]:
        values = [str(value if value is not None else "").strip() for value in row[:MAX_SHEET_COLS]]
        if any(values):
            trimmed.append(values)
    if not trimmed:
        return ""
    text = _table_markdown(trimmed)
    if len(rows) > MAX_SHEET_ROWS:
        text += f"\n\n> 表格超过 {MAX_SHEET_ROWS} 行，已截断"
    return text


def parse_xlsx(path: Path) -> ParsedDoc:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ParserUnavailable(
            "解析 XLSX 需要 openpyxl：pip install openpyxl"
        ) from exc

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    blocks = [f"# {path.stem}"]
    try:
        for sheet in workbook.worksheets:
            rows = [[cell for cell in row] for row in sheet.iter_rows(values_only=True)]
            table = _sheet_markdown(rows)
            if table:
                blocks.append(f"## 工作表：{sheet.title}\n{table}")
    finally:
        workbook.close()
    if len(blocks) == 1:
        raise ValueError(f"{path.name}: 工作簿没有可提取的文本")
    return ParsedDoc(title=path.stem, text="\n\n".join(blocks), doc_type="sheet")


def parse_xls(path: Path) -> ParsedDoc:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise ParserUnavailable(
            "解析 XLS 需要 xlrd：pip install xlrd"
        ) from exc

    workbook = xlrd.open_workbook(str(path), on_demand=True)
    blocks = [f"# {path.stem}"]
    try:
        for sheet in workbook.sheets():
            rows = [sheet.row_values(i) for i in range(sheet.nrows)]
            table = _sheet_markdown(rows)
            if table:
                blocks.append(f"## 工作表：{sheet.name}\n{table}")
    finally:
        workbook.release_resources()
    if len(blocks) == 1:
        raise ValueError(f"{path.name}: 工作簿没有可提取的文本")
    return ParsedDoc(title=path.stem, text="\n\n".join(blocks), doc_type="sheet")


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
register({".pptx"}, parse_pptx)
register({".xlsx", ".xlsm"}, parse_xlsx)
register({".xls"}, parse_xls)
