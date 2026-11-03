"""
FR1 — PDF 简历文本提取。

策略：优先 pdfplumber；失败或文本过短时回退 PyMuPDF。
疑似扫描件（文本 < 阈值）明确报错，不静默继续。
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from config.llm_config import get_settings
from src.models.schemas import ParseResult
from src.parsers.exceptions import ParseError

BytesOrPath = Union[str, Path, bytes]


def _merge_lines(lines: list[str]) -> str:
    """简单行合并：去掉多余空白，保留段落感。"""
    merged: list[str] = []
    for line in lines:
        s = " ".join(line.split())
        if s:
            merged.append(s)
    return "\n".join(merged)


def _extract_with_pdfplumber(data: bytes) -> tuple[str, int, list[str]]:
    import io

    import pdfplumber

    warnings: list[str] = []
    pages_text: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            parts: list[str] = []
            text = page.extract_text() or ""
            if text.strip():
                parts.append(text)
            # 表格单独提取，避免丢失
            try:
                tables = page.extract_tables() or []
                for table in tables:
                    for row in table:
                        cells = [str(c).strip() if c else "" for c in row]
                        if any(cells):
                            parts.append(" | ".join(cells))
            except Exception:
                warnings.append(f"第{i + 1}页表格提取失败，已跳过表格")
            pages_text.append(_merge_lines("\n".join(parts).splitlines()))
    return "\n\n".join(p for p in pages_text if p), page_count, warnings


def _extract_with_pymupdf(data: bytes) -> tuple[str, int, list[str]]:
    import fitz  # PyMuPDF

    warnings: list[str] = []
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        pages_text: list[str] = []
        for page in doc:
            text = page.get_text("text") or ""
            pages_text.append(_merge_lines(text.splitlines()))
        return "\n\n".join(p for p in pages_text if p), doc.page_count, warnings
    finally:
        doc.close()


def _check_column_disorder(text: str) -> list[str]:
    """粗检多栏乱序：单行极短且相邻行跳跃性大时给出提示。"""
    warnings: list[str] = []
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 20:
        return warnings
    short = sum(1 for ln in lines if len(ln) <= 4)
    if short / len(lines) > 0.45:
        warnings.append(
            "检测到可能的多栏排版乱序，建议核对简历文本；严重时可改用纯文本版简历。"
        )
    return warnings


def parse_pdf_resume(data: bytes, *, filename: str = "resume.pdf") -> ParseResult:
    """解析 PDF 简历，返回归一化纯文本。"""
    settings = get_settings()
    warnings: list[str] = []
    text = ""
    page_count = 0

    try:
        text, page_count, w = _extract_with_pdfplumber(data)
        warnings.extend(w)
    except Exception as e:
        warnings.append(f"pdfplumber 解析异常，尝试 PyMuPDF 回退：{e}")

    if len(text.strip()) < settings.pdf_min_text_chars:
        try:
            text2, page_count2, w2 = _extract_with_pymupdf(data)
            warnings.extend(w2)
            if len(text2.strip()) > len(text.strip()):
                text, page_count = text2, page_count2
        except Exception as e:
            warnings.append(f"PyMuPDF 回退失败：{e}")

    text = text.strip()
    if len(text) < settings.pdf_min_text_chars:
        raise ParseError(
            "请上传可复制文本版本或启用 OCR（当前 PDF 疑似扫描件/几乎无字）",
            code="SUSPECTED_SCAN",
        )

    warnings.extend(_check_column_disorder(text))
    return ParseResult(
        text=text,
        source_type="resume_pdf",
        page_count=page_count,
        warnings=warnings,
        ok=True,
    )
