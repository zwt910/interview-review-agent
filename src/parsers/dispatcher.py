"""按文件扩展名分发到对应解析器。"""

from __future__ import annotations

from pathlib import Path

from src.models.schemas import ParseResult
from src.parsers.docx_parser import parse_dialogue_docx, parse_resume_docx
from src.parsers.exceptions import ParseError
from src.parsers.pdf_parser import parse_pdf_resume


def extract_text_from_file(
    data: bytes,
    filename: str,
    *,
    kind: str,
) -> ParseResult:
    """统一出口：归一为纯文本。

    Args:
        data: 文件字节
        filename: 原始文件名（用于判断扩展名）
        kind: "resume" | "dialogue"
    """
    suffix = Path(filename).suffix.lower()
    if kind == "resume":
        if suffix == ".pdf":
            return parse_pdf_resume(data, filename=filename)
        if suffix == ".docx":
            return parse_resume_docx(data, filename=filename)
        raise ParseError(
            f"简历仅支持 .pdf / .docx，收到：{suffix or '(无扩展名)'}",
            code="UNSUPPORTED_FORMAT",
        )

    if kind == "dialogue":
        if suffix == ".docx":
            return parse_dialogue_docx(data, filename=filename)
        raise ParseError(
            f"对话文档仅支持 .docx，收到：{suffix or '(无扩展名)'}",
            code="UNSUPPORTED_FORMAT",
        )

    raise ParseError(f"未知 kind: {kind}", code="BAD_KIND")
