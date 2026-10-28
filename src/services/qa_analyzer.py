"""
FR5 — 逐题分析。

支持有/无 JD 双提示词；可按完成顺序回调，便于 SSE 推送。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Optional

from config.llm_config import get_settings
from prompts import render_prompt
from src.llm.client import LLMClient, get_llm_client_for
from src.models.schemas import AnalysisResult, QAItem, ResumeSummary
from src.services.resume_summary import resume_summary_to_json_str
from src.utils.resume_notes import format_resume_notes_block, normalize_resume_notes

logger = logging.getLogger(__name__)

OnQADone = Callable[[int, int, QAItem, AnalysisResult], Awaitable[None]]

_SECTION_RE = re.compile(
    r"###\s*([1-4])[\.、．]?\s*(?P<title>[^\n]*)\n(?P<body>[\s\S]*?)(?=\n###\s*[1-4][\.、．]?|\Z)"
)


def _parse_analysis_markdown(md: str, qa_id: str, position: str) -> AnalysisResult:
    sections: dict[str, str] = {}
    for m in _SECTION_RE.finditer(md):
        sections[m.group(1)] = m.group("body").strip()

    assessment = sections.get("1", "")
    original = sections.get("2", "")
    issues_raw = sections.get("3", "")
    improved = sections.get("4", "")

    issues: list[str] = []
    for line in issues_raw.splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^[-*•]\s*", "", s)
        if s:
            issues.append(s)

    resume_refs = re.findall(
        r"「([^」]{2,40})」|【([^】]{2,40})】",
        improved + "\n" + issues_raw,
    )
    refs = [a or b for a, b in resume_refs]
    success = bool(assessment or improved or issues)
    return AnalysisResult(
        qa_id=qa_id,
        assessment_point=assessment,
        original_answer=original,
        issues=issues,
        improved_answer=improved,
        resume_refs=refs,
        position=position,
        raw_markdown=md,
        success=success,
        error=None if success else "未能解析出有效四段结构",
    )


def _build_prompt(
    qa: QAItem,
    *,
    resume_summary_json: str,
    position: str,
    job_description: str = "",
    resume_notes: str = "",
) -> str:
    pos = position or "待确认"
    jd = (job_description or "").strip()
    notes_block = format_resume_notes_block(normalize_resume_notes(resume_notes))
    if jd:
        return render_prompt(
            "qa_analysis_with_jd",
            resume_summary_json=resume_summary_json,
            resume_notes_block=notes_block,
            position=pos,
            job_description=jd,
            question=qa.question,
            answer=qa.answer,
        )
    return render_prompt(
        "qa_analysis",
        resume_summary_json=resume_summary_json,
        resume_notes_block=notes_block,
        position=pos,
        question=qa.question,
        answer=qa.answer,
    )


async def _analyze_one(
    qa: QAItem,
    *,
    resume_summary_json: str,
    position: str,
    job_description: str,
    resume_notes: str = "",
    client: LLMClient,
    semaphore: asyncio.Semaphore,
) -> AnalysisResult:
    async with semaphore:
        prompt = _build_prompt(
            qa,
            resume_summary_json=resume_summary_json,
            position=position,
            job_description=job_description,
            resume_notes=resume_notes,
        )
        try:
            raw = await client.achat(
                prompt,
                system="你是面试复盘分析助手，严格按四段 markdown 输出。",
            )
            return _parse_analysis_markdown(raw, qa.id, position or "待确认")
        except Exception as e:
            logger.error("题目 %s 分析失败: %s", qa.id, e)
            return AnalysisResult(
                qa_id=qa.id,
                assessment_point="",
                original_answer=qa.answer,
                issues=[],
                improved_answer="",
                position=position or "待确认",
                raw_markdown=qa.answer,
                success=False,
                error=str(e),
            )


async def analyze_qa_batch_async(
    qa_items: list[QAItem],
    resume_summary: Optional[ResumeSummary],
    *,
    position: str = "待确认",
    job_description: str = "",
    resume_notes: str = "",
    client: Optional[LLMClient] = None,
    concurrency: Optional[int] = None,
    on_qa_done: Optional[OnQADone] = None,
) -> list[AnalysisResult]:
    """有限并发分析；每完成一题可回调（按完成顺序，非题目序号顺序）。

    为满足「完成一题推一题」，结果列表按 qa_items 原始顺序组装；
    回调在单题完成时立即触发。
    """
    settings = get_settings()
    llm = client or get_llm_client_for("analysis")
    conc = concurrency or settings.qa_analysis_concurrency
    semaphore = asyncio.Semaphore(conc)
    notes = normalize_resume_notes(resume_notes)

    if resume_summary is None:
        resume_json = '{"提示": "无简历参照，改进回答受限"}'
    else:
        resume_json = resume_summary_to_json_str(resume_summary)

    total = len(qa_items)
    results: list[Optional[AnalysisResult]] = [None] * total
    completed = 0
    lock = asyncio.Lock()

    async def _run(idx: int, qa: QAItem) -> None:
        nonlocal completed
        analysis = await _analyze_one(
            qa,
            resume_summary_json=resume_json,
            position=position,
            job_description=job_description,
            resume_notes=notes,
            client=llm,
            semaphore=semaphore,
        )
        results[idx] = analysis
        async with lock:
            completed += 1
            done_count = completed
        if on_qa_done:
            await on_qa_done(idx, done_count, qa, analysis)

    await asyncio.gather(*[_run(i, qa) for i, qa in enumerate(qa_items)])
    return [r for r in results if r is not None]


def analyze_qa_batch(
    qa_items: list[QAItem],
    resume_summary: Optional[ResumeSummary],
    *,
    position: str = "待确认",
    job_description: str = "",
    resume_notes: str = "",
    client: Optional[LLMClient] = None,
    concurrency: Optional[int] = None,
) -> list[AnalysisResult]:
    return asyncio.run(
        analyze_qa_batch_async(
            qa_items,
            resume_summary,
            position=position,
            job_description=job_description,
            resume_notes=resume_notes,
            client=client,
            concurrency=concurrency,
        )
    )
