"""
FR3 — 对话整理，生成 interview_qa.md。

调用 prompts/dialogue_organize.txt。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from prompts import render_prompt
from src.llm.client import LLMClient, get_llm_client_for

logger = logging.getLogger(__name__)


def _strip_fence(text: str) -> str:
    """去掉模型可能包裹的 markdown 代码围栏。"""
    text = text.strip()
    m = re.match(r"^```(?:markdown|md)?\s*([\s\S]*?)```\s*$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


def _looks_like_qa_markdown(text: str) -> bool:
    return bool(re.search(r"^##\s*Q\d+", text, re.MULTILINE)) and ("**问：**" in text or "**问:**" in text)


def organize_dialogue(
    dialogue_text: str,
    *,
    client: Optional[LLMClient] = None,
) -> str:
    """整理对话为 interview_qa.md 内容。

    Raises:
        ValueError: 对话为空或模型输出无法形成有效问答 markdown
    """
    if not dialogue_text or not dialogue_text.strip():
        raise ValueError("对话文本为空，无法整理问答")

    llm = client or get_llm_client_for("dialogue")
    prompt = render_prompt("dialogue_organize", dialogue_text=dialogue_text.strip())

    raw = llm.chat(prompt, system="你只输出规定格式的 markdown 问答，不要额外解释。")
    md = _strip_fence(raw)

    if not _looks_like_qa_markdown(md):
        logger.warning("对话整理输出格式异常，尝试修复一次")
        fix_prompt = (
            "请将下面内容改写为严格的面试问答 markdown，格式为：\n"
            "## Q1｜话题：xxx\n"
            "- 追问自：Q0（可选）\n"
            "**问：** ...\n"
            "**答：** ...\n\n"
            "只输出 markdown：\n\n"
            + md[:8000]
        )
        md = _strip_fence(llm.chat(fix_prompt, temperature=0))

    if not _looks_like_qa_markdown(md):
        raise ValueError("对话无有效问答或模型输出格式错误，已终止")

    return md
