"""
FR2 — 简历结构化摘要。

调用 prompts/resume_summary.txt；JSON 解析失败则降级为纯文本摘要。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

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


def _clip_str(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit].rstrip() + "…"
    return value


def _compact_mapping(data: Any, *, str_limit: int = 80, max_keys: int = 8) -> Any:
    if not isinstance(data, dict):
        return _clip_str(data, str_limit)
    out: dict[str, Any] = {}
    for i, (k, v) in enumerate(data.items()):
        if i >= max_keys:
            break
        if isinstance(v, dict):
            out[str(k)] = _compact_mapping(v, str_limit=str_limit, max_keys=6)
        elif isinstance(v, list):
            out[str(k)] = [
                _clip_str(x, str_limit) if not isinstance(x, dict) else _compact_mapping(x, str_limit=str_limit)
                for x in v[:4]
            ]
        else:
            out[str(k)] = _clip_str(v, str_limit)
    return out


def _compact_experience_or_project(item: Any) -> Any:
    if not isinstance(item, dict):
        return _clip_str(item, 100)
    preferred = (
        "公司",
        "company",
        "职位",
        "title",
        "role",
        "时间",
        "period",
        "项目",
        "name",
        "名称",
        "职责",
        "描述",
        "description",
        "成果",
        "结果",
    )
    slim: dict[str, Any] = {}
    for key in preferred:
        if key in item and item[key] not in (None, "", []):
            slim[key] = _clip_str(item[key], 120) if not isinstance(item[key], (dict, list)) else _compact_mapping(item[key])
    if not slim:
        slim = _compact_mapping(item, str_limit=100, max_keys=6)
    return slim


def resume_summary_to_compact_json_str(
    summary: ResumeSummary,
    *,
    max_chars: int = 2200,
) -> str:
    """供逐题分析使用的压缩简历 JSON（去缩进、截断长字段），降低重复注入的 token。"""
    if summary.is_fallback_text and summary.raw_text_fallback:
        payload = {"摘要文本": summary.raw_text_fallback[:1200]}
    else:
        payload = {
            "基本信息": _compact_mapping(summary.basic, str_limit=60, max_keys=8),
            "教育": [_compact_mapping(x, str_limit=60) for x in (summary.education or [])[:3]],
            "工作经历": [_compact_experience_or_project(x) for x in (summary.experiences or [])[:4]],
            "项目": [_compact_experience_or_project(x) for x in (summary.projects or [])[:4]],
            "技能": [_clip_str(s, 40) for s in (summary.skills or [])[:12]],
            "量化成果": [_clip_str(s, 80) for s in (summary.quantified_results or [])[:6]],
            "亮点": [_clip_str(s, 80) for s in (summary.highlights or [])[:5]],
        }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
