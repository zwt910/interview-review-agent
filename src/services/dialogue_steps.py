"""
需求3：对话解析三步（转写 → 澄清问题 → 生成 QA）。
均使用 dialogue 阶段模型配置。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from prompts import render_prompt
from src.llm.client import LLMClient, get_llm_client_for
from src.models.schemas import ResumeSummary
from src.services.resume_summary import resume_summary_to_json_str
from src.utils.resume_notes import format_resume_notes_block, normalize_resume_notes

logger = logging.getLogger(__name__)


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    m = re.match(r"^```(?:markdown|md|text)?\s*([\s\S]*?)```\s*$", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


def transcribe_dialogue(
    dialogue_text: str,
    *,
    client: Optional[LLMClient] = None,
) -> str:
    """步骤2：转写清洗文稿。"""
    if not dialogue_text.strip():
        raise ValueError("对话文本为空")
    llm = client or get_llm_client_for("dialogue")
    prompt = render_prompt("dialogue_transcribe", dialogue_text=dialogue_text.strip())
    raw = llm.chat(prompt, system="你只输出清洗后的面试文稿正文。")
    text = _strip_fence(raw)
    if len(text) < 20:
        raise ValueError("转写结果过短，请重试")
    return text


def clarify_questions(
    transcript_text: str,
    resume_summary: Optional[ResumeSummary],
    *,
    resume_notes: str = "",
    client: Optional[LLMClient] = None,
) -> str:
    """步骤3：澄清完整问题列表。"""
    if not transcript_text.strip():
        raise ValueError("转写文稿为空")
    llm = client or get_llm_client_for("dialogue")
    if resume_summary is None:
        resume_json = '{"提示": "无简历参照"}'
    else:
        resume_json = resume_summary_to_json_str(resume_summary)
    prompt = render_prompt(
        "dialogue_clarify_questions",
        resume_summary_json=resume_json,
        resume_notes_block=format_resume_notes_block(normalize_resume_notes(resume_notes)),
        transcript_text=transcript_text.strip(),
    )
    raw = llm.chat(prompt, system="你只输出编号的问题列表 markdown。")
    text = _strip_fence(raw)
    if not re.search(r"^\s*\d+[\.、．]", text, re.MULTILINE):
        logger.warning("问题列表格式异常，尝试修复")
        fix = llm.chat(
            "请把下面内容改写为编号列表（1. 2. 3. …），每行一个完整问题，只输出列表：\n\n"
            + text[:8000],
            temperature=0,
        )
        text = _strip_fence(fix)
    if not re.search(r"^\s*\d+[\.、．]", text, re.MULTILINE):
        raise ValueError("未能解析出有效问题列表")
    return text


def build_qa_from_questions(
    questions_text: str,
    transcript_text: str,
    *,
    client: Optional[LLMClient] = None,
) -> str:
    """步骤4：由问题列表 + 转写文稿生成 interview_qa.md。"""
    if not questions_text.strip():
        raise ValueError("问题列表为空")
    if not transcript_text.strip():
        raise ValueError("转写文稿为空")
    llm = client or get_llm_client_for("dialogue")
    prompt = render_prompt(
        "dialogue_build_qa",
        questions_text=questions_text.strip(),
        transcript_text=transcript_text.strip(),
    )
    raw = llm.chat(prompt, system="你只输出规定格式的 markdown 问答。")
    md = _strip_fence(raw)
    if not re.search(r"^##\s*Q\d+", md, re.MULTILINE):
        logger.warning("QA markdown 格式异常，尝试修复")
        fix = llm.chat(
            "请将下面内容改写为严格的面试问答 markdown：\n"
            "## Q1｜话题：xxx\n**问：** ...\n**答：** ...\n\n只输出 markdown：\n\n"
            + md[:8000],
            temperature=0,
        )
        md = _strip_fence(fix)
    if not re.search(r"^##\s*Q\d+", md, re.MULTILINE):
        raise ValueError("未能生成有效问答 markdown")
    return md
