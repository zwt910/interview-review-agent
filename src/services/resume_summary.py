"""
FR2 — 简历结构化摘要。

调用 prompts/resume_summary.txt；JSON 解析失败则降级为纯文本摘要。
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from prompts import render_prompt
from src.llm.client import LLMClient, get_llm_client_for
from src.models.schemas import ResumeSummary
from src.utils.json_utils import extract_json_object
from src.utils.resume_notes import format_resume_notes_block, normalize_resume_notes

logger = logging.getLogger(__name__)


def summarize_resume(
    resume_text: str,
    *,
    resume_notes: str = "",
    client: Optional[LLMClient] = None,
) -> ResumeSummary:
    """将简历纯文本转为 ResumeSummary；可选融合用户【简历注释】。"""
    if not resume_text or not resume_text.strip():
        return ResumeSummary.from_fallback_text("（简历文本为空）")

    llm = client or get_llm_client_for("resume")
    notes = normalize_resume_notes(resume_notes)
    prompt = render_prompt(
        "resume_summary",
        resume_text=resume_text.strip(),
        resume_notes_block=format_resume_notes_block(notes),
    )

    try:
        raw = llm.chat(prompt, system="你只输出合法 JSON，不要 markdown 代码块。")
    except Exception as e:
        logger.error("简历摘要 LLM 调用失败，降级为纯文本: %s", e)
        return ResumeSummary.from_fallback_text(resume_text)

    data = extract_json_object(raw)
    if data is None:
        logger.warning("简历摘要 JSON 解析失败，降级为纯文本摘要")
        # 再试一次：让模型修复
        try:
            fix_prompt = (
                "请把下面内容修复为合法 JSON 对象（仅输出 JSON）：\n\n" + raw[:6000]
            )
            fixed = llm.chat(fix_prompt, temperature=0)
            data = extract_json_object(fixed)
        except Exception as e:
            logger.warning("JSON 修复失败: %s", e)

    if data is None:
        return ResumeSummary.from_fallback_text(resume_text)

    try:
        return ResumeSummary.from_llm_json(data)
    except Exception as e:
        logger.warning("ResumeSummary 构造失败: %s", e)
        return ResumeSummary.from_fallback_text(resume_text)


def resume_summary_to_json_str(summary: ResumeSummary) -> str:
    return json.dumps(summary.to_prompt_json(), ensure_ascii=False, indent=2)
