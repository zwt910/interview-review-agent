"""通用文本工具。"""

from __future__ import annotations

import re
from typing import Any, Optional


def truncate(text: str, max_len: int = 500) -> str:
    text = text or ""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def infer_position(
    resume_summary: Optional[dict[str, Any]] = None,
    dialogue_text: str = "",
    resume_text: str = "",
) -> str:
    """从简历/对话中粗略推断目标岗位；无法确定返回「待确认」。"""
    candidates: list[str] = []

    if resume_summary:
        basic = resume_summary.get("基本信息") or resume_summary.get("basic") or {}
        if isinstance(basic, dict):
            intent = basic.get("求职意向") or basic.get("意向岗位")
            if intent:
                candidates.append(str(intent).strip())

    patterns = [
        r"求职意向[：:\s]*([^\n，,；;]{2,30})",
        r"应聘岗位[：:\s]*([^\n，,；;]{2,30})",
        r"职位[：:\s]*([^\n，,；;]{2,30})",
        r"(?:招聘|面试)(?:岗位|职位)[：:\s]*([^\n，,；;]{2,30})",
    ]
    blob = f"{resume_text or ''}\n{dialogue_text or ''}"
    for pat in patterns:
        m = re.search(pat, blob)
        if m:
            candidates.append(m.group(1).strip())

    for c in candidates:
        if c and c.lower() not in ("null", "none", "n/a"):
            return c
    return "待确认"
