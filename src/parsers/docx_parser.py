"""
FR1 — Word（.docx）文本提取。

遍历段落与表格；尽量保留标题/列表结构；对话文档保留说话人前缀。
"""

from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.models.schemas import ParseResult
from src.parsers.exceptions import ParseError


def _is_list_paragraph(paragraph: Paragraph) -> bool:
    """判断是否为列表项（含编号/项目符号）。"""
    try:
        p = paragraph._p
        numPr = p.find(qn("w:pPr"))
        if numPr is not None and numPr.find(qn("w:numPr")) is not None:
            return True
    except Exception:
        pass
    style_name = (paragraph.style.name or "") if paragraph.style else ""
    return "List" in style_name or "列表" in style_name


def _format_paragraph(paragraph: Paragraph) -> str:
    text = (paragraph.text or "").strip()
    if not text:
        return ""

    style_name = ""
    try:
        if paragraph.style and paragraph.style.type == WD_STYLE_TYPE.PARAGRAPH:
            style_name = paragraph.style.name or ""
    except Exception:
        style_name = ""

    # 标题
    if style_name.startswith("Heading") or style_name.startswith("标题"):
        level = 1
        for ch in style_name:
            if ch.isdigit():
                level = int(ch)
                break
        return f"{'#' * min(level, 6)} {text}"

    if _is_list_paragraph(paragraph):
        return f"- {text}"

    return text


def _iter_block_items(doc: Document):
    """按文档顺序产出段落与表格。"""
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield Table(child, doc)


def _table_to_text(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [" ".join((c.text or "").split()) for c in row.cells]
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def parse_docx(data: bytes, *, source_type: str, filename: str = "doc.docx") -> ParseResult:
    """解析 docx，返回保留结构提示的纯文本。"""
    try:
        doc = Document(BytesIO(data))
    except Exception as e:
        raise ParseError(f"Word 文档无法打开：{e}", code="DOCX_OPEN_FAIL") from e

    parts: list[str] = []
    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            line = _format_paragraph(block)
            if line:
                parts.append(line)
        elif isinstance(block, Table):
            t = _table_to_text(block)
            if t:
                parts.append(t)

    text = "\n".join(parts).strip()
    if not text:
        raise ParseError(
            "Word 文档提取结果为空（可能为图片型文档或内容无法读取）",
            code="EMPTY_TEXT",
        )

    return ParseResult(
        text=text,
        source_type=source_type,
        warnings=[],
        ok=True,
    )


def parse_resume_docx(data: bytes, *, filename: str = "resume.docx") -> ParseResult:
    return parse_docx(data, source_type="resume_docx", filename=filename)


def parse_dialogue_docx(data: bytes, *, filename: str = "dialogue.docx") -> ParseResult:
    """对话文档：同样走段落遍历，说话人前缀由原文保留。"""
    return parse_docx(data, source_type="dialogue_docx", filename=filename)
