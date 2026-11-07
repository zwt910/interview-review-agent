"""
FR5 — 逐题分析。

每题两次串行调用大模型（省 token）：
1) 精简上下文，只析考察点（不含原答、不含简历全文）；
2) 压缩简历 + 考察点 + 原答，一次输出「不妥之处」与「改进回答」。
「原文回答」段直接使用候选人原答。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Optional

from config.llm_config import get_llm_config_analysis, get_settings
from prompts import render_prompt
from src.llm.client import LLMClient, get_llm_client_for
from src.models.schemas import AnalysisResult, QAItem, ResumeSummary
from src.services.context_compress import format_compressed_notes_block
from src.services.resume_summary import resume_summary_to_compact_json_str
from src.utils.request_context import with_ctx
from src.utils.resume_notes import normalize_resume_notes

logger = logging.getLogger(__name__)

OnQADone = Callable[[int, int, QAItem, AnalysisResult], Awaitable[None]]

_SECTION_RE = re.compile(
    r"###\s*([1-4])[\.、．]?\s*(?P<title>[^\n]*)\n(?P<body>[\s\S]*?)(?=\n###\s*[1-4][\.、．]?|\Z)"
)

# 语义压缩后的保险丝（第 1 步 JD 再略短，避免考察点 prompt 过长）
_JD_STEP1_FUSE = 1200
_JD_STEP2_FUSE = 2200
_NOTES_STEP2_FUSE = 1400


def analysis_to_prompt_markdown(a: AnalysisResult, qa: Optional[QAItem] = None) -> str:
    """把已有分析结果格式化为提示词中的「上一版」正文。"""
    if a.assessment_point or a.improved_answer or a.issues:
        issues = "\n".join(f"- {i}" for i in (a.issues or [])) or "_（无）_"
        return "\n".join(
            [
                "### 1. 问题考察点",
                "",
                a.assessment_point or "_（空）_",
                "",
                "### 2. 原文回答",
                "",
                a.original_answer or (qa.answer if qa else "") or "_（空）_",
                "",
                "### 3. 原回答不妥之处",
                "",
                issues,
                "",
                "### 4. 合理回答（改进示例）",
                "",
                a.improved_answer or "_（空）_",
            ]
        )
    return (a.raw_markdown or "").strip() or "_（上一版为空）_"


def _clip_text(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if max_chars <= 0 or len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _extract_section_body(md: str, section_num: str) -> str:
    """从模型输出中提取指定 ### N 段正文；若无标题则回退为全文去空白。"""
    text = (md or "").strip()
    if not text:
        return ""
    for m in _SECTION_RE.finditer(text):
        if m.group(1) == section_num:
            return m.group("body").strip()
    return text


def _parse_issues_list(issues_raw: str) -> list[str]:
    issues: list[str] = []
    for line in (issues_raw or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^[-*•]\s*", "", s)
        if s:
            issues.append(s)
    return issues


def _assemble_analysis_markdown(
    *,
    assessment: str,
    original: str,
    issues: list[str],
    improved: str,
) -> str:
    issues_md = "\n".join(f"- {i}" for i in issues) if issues else "_（未识别到明确不妥之处）_"
    return "\n".join(
        [
            "### 1. 问题考察点",
            "",
            assessment.strip() or "_（空）_",
            "",
            "### 2. 原文回答",
            "",
            original.strip() or "_（空）_",
            "",
            "### 3. 原回答不妥之处",
            "",
            issues_md,
            "",
            "### 4. 合理回答（改进示例）",
            "",
            improved.strip() or "_（空）_",
        ]
    )


def _parse_analysis_markdown(md: str, qa_id: str, position: str) -> AnalysisResult:
    """兼容旧一站式四段输出（如单题修订仍可能一次返回四段）。"""
    sections: dict[str, str] = {}
    for m in _SECTION_RE.finditer(md):
        sections[m.group(1)] = m.group("body").strip()

    assessment = sections.get("1", "")
    original = sections.get("2", "")
    issues_raw = sections.get("3", "")
    improved = sections.get("4", "")

    issues = _parse_issues_list(issues_raw)

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
        version=1,
        created_at=datetime.now().isoformat(timespec="seconds"),
    )


def _prompt_name(base: str, *, has_jd: bool) -> str:
    return f"{base}_with_jd" if has_jd else base


def _analysis_resume_json(resume_summary: Optional[ResumeSummary]) -> str:
    if resume_summary is None:
        return '{"提示":"无简历参照，改进回答受限"}'
    return resume_summary_to_compact_json_str(resume_summary)


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
    """单题两步串行分析；job_description / resume_notes 应为会话级语义压缩后的 JSON 字符串。"""
    pos = position or "待确认"
    jd_full = (job_description or "").strip()
    notes_json = (resume_notes or "").strip()
    analysis_cfg = get_llm_config_analysis()
    step1_max = int(getattr(analysis_cfg, "step1_max_tokens", None) or analysis_cfg.max_tokens)
    step2_max = int(getattr(analysis_cfg, "step2_max_tokens", None) or analysis_cfg.max_tokens)

    async with semaphore:
        try:
            # —— 第一步：考察点（无原答、无简历；JD 用压缩结果 + 保险丝）——
            jd1 = _clip_text(jd_full, _JD_STEP1_FUSE)
            has_jd1 = bool(jd1)
            p1 = render_prompt(
                _prompt_name("qa_assessment", has_jd=has_jd1),
                position=pos,
                question=qa.question,
                job_description=jd1,
            )
            raw1 = await client.achat(
                p1,
                system="你是面试复盘分析助手，只输出「问题考察点」一段 markdown。",
                max_tokens=step1_max,
            )
            assessment = _extract_section_body(raw1, "1")
            if not assessment:
                raise ValueError("第一步未能解析出问题考察点")
            logger.info(
                with_ctx("题目 %s 分析步骤1/2 考察点完成 chars=%s prompt_chars≈%s"),
                qa.id,
                len(assessment),
                len(p1),
            )

            # —— 第二步：不妥之处 + 改进回答（语义压缩简历/注释/JD + 考察点 + 原答）——
            jd2 = _clip_text(jd_full, _JD_STEP2_FUSE)
            has_jd2 = bool(jd2)
            notes_block = format_compressed_notes_block(_clip_text(notes_json, _NOTES_STEP2_FUSE))
            p2 = render_prompt(
                _prompt_name("qa_critique_improve", has_jd=has_jd2),
                resume_summary_json=resume_summary_json,
                resume_notes_block=notes_block,
                position=pos,
                job_description=jd2,
                question=qa.question,
                answer=qa.answer,
                assessment_point=assessment,
            )
            raw2 = await client.achat(
                p2,
                system="你是面试复盘分析助手，只输出「原回答不妥之处」与「合理回答」两段 markdown。",
                max_tokens=step2_max,
            )
            issues_raw = _extract_section_body(raw2, "3")
            # 若模型把两段都合在一起，优先用正则分节；否则整段当 issues 风险高——再扫一遍 section 4
            sections: dict[str, str] = {}
            for m in _SECTION_RE.finditer(raw2 or ""):
                sections[m.group(1)] = m.group("body").strip()
            if "3" in sections:
                issues_raw = sections["3"]
            improved = sections.get("4") or _extract_section_body(raw2, "4")
            # 若只有一段且未标号，尝试按标题关键字切分
            if not sections and raw2:
                issues_raw = _extract_section_body(raw2, "3")
                improved = _extract_section_body(raw2, "4")

            issues = _parse_issues_list(issues_raw)
            if not issues and issues_raw.strip():
                issues = [issues_raw.strip()]
            if not improved:
                raise ValueError("第二步未能解析出改进回答")
            logger.info(
                with_ctx(
                    "题目 %s 分析步骤2/2 不妥+改进完成 issues=%s improved_chars=%s prompt_chars≈%s"
                ),
                qa.id,
                len(issues),
                len(improved),
                len(p2),
            )

            original = qa.answer or ""
            md = _assemble_analysis_markdown(
                assessment=assessment,
                original=original,
                issues=issues,
                improved=improved,
            )
            resume_refs = re.findall(
                r"「([^」]{2,40})」|【([^】]{2,40})】",
                improved + "\n" + "\n".join(issues),
            )
            refs = [a or b for a, b in resume_refs]
            return AnalysisResult(
                qa_id=qa.id,
                assessment_point=assessment,
                original_answer=original,
                issues=issues,
                improved_answer=improved,
                resume_refs=refs,
                position=pos,
                raw_markdown=md,
                success=True,
                error=None,
                version=1,
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as e:
            logger.error(with_ctx("题目 %s 分析失败: %s"), qa.id, e)
            return AnalysisResult(
                qa_id=qa.id,
                assessment_point="",
                original_answer=qa.answer,
                issues=[],
                improved_answer="",
                position=pos,
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
    """有限并发分析；每完成一题可回调（按完成顺序，非题目序号顺序）。"""
    settings = get_settings()
    llm = client or get_llm_client_for("analysis")
    conc = concurrency or settings.qa_analysis_concurrency
    semaphore = asyncio.Semaphore(conc)
    notes = normalize_resume_notes(resume_notes)
    resume_json = _analysis_resume_json(resume_summary)

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


def _step2_reference_markdown(a: AnalysisResult) -> str:
    """修订时仅把上一版第 3/4 段作为参考，考察点单独注入且保持不变。"""
    issues = "\n".join(f"- {i}" for i in (a.issues or [])) or "_（无）_"
    return "\n".join(
        [
            "### 3. 原回答不妥之处",
            "",
            issues,
            "",
            "### 4. 合理回答（改进示例）",
            "",
            a.improved_answer or "_（空）_",
        ]
    )


async def revise_one(
    qa: QAItem,
    *,
    base_analysis: AnalysisResult,
    user_feedback: str,
    resume_summary: Optional[ResumeSummary],
    position: str = "待确认",
    job_description: str = "",
    resume_notes: str = "",
    resume_summary_json: Optional[str] = None,
    client: Optional[LLMClient] = None,
) -> AnalysisResult:
    """单题修订：沿用基版「问题考察点」，仅按用户要求重跑第二步（不妥+改进）。

    job_description / resume_notes 建议传入会话级语义压缩 JSON；
    resume_summary_json 若提供则优先于 resume_summary。
    """
    feedback = (user_feedback or "").strip()
    if not feedback:
        raise ValueError("用户修改要求不能为空")

    assessment = (base_analysis.assessment_point or "").strip()
    if not assessment:
        raise ValueError("基版缺少问题考察点，无法修订第二步")

    llm = client or get_llm_client_for("analysis")
    analysis_cfg = get_llm_config_analysis()
    step2_max = int(getattr(analysis_cfg, "step2_max_tokens", None) or analysis_cfg.max_tokens)
    # 优先使用调用方传入的会话级压缩 JSON；否则临时程序压缩简历
    resume_json = (resume_summary_json or "").strip() or _analysis_resume_json(resume_summary)
    jd = _clip_text((job_description or "").strip(), _JD_STEP2_FUSE)
    jd_block = f"\n【岗位 JD（语义压缩）】\n{jd}\n" if jd else ""
    notes_block = format_compressed_notes_block(
        _clip_text((resume_notes or "").strip(), _NOTES_STEP2_FUSE)
    )

    pos = position or "待确认"

    prompt = render_prompt(
        "qa_revise",
        resume_summary_json=resume_json,
        resume_notes_block=notes_block,
        position=pos,
        job_description_block=jd_block,
        question=qa.question,
        answer=qa.answer,
        base_version=str(base_analysis.version or 1),
        assessment_point=assessment,
        previous_step2_markdown=_step2_reference_markdown(base_analysis),
        user_feedback=feedback,
    )

    logger.info(
        with_ctx(
            "单题修订开始（仅重跑第二步） qa_id=%s base_version=%s feedback_chars=%s"
        ),
        qa.id,
        base_analysis.version or 1,
        len(feedback),
    )
    try:
        raw = await llm.achat(
            prompt,
            system="你是面试复盘分析助手，只输出「原回答不妥之处」与「合理回答」两段 markdown；不要改写考察点。",
            max_tokens=step2_max,
        )
        sections: dict[str, str] = {}
        for m in _SECTION_RE.finditer(raw or ""):
            sections[m.group(1)] = m.group("body").strip()
        issues_raw = sections.get("3") or _extract_section_body(raw, "3")
        improved = sections.get("4") or _extract_section_body(raw, "4")
        issues = _parse_issues_list(issues_raw)
        if not issues and issues_raw.strip():
            issues = [issues_raw.strip()]
        if not improved:
            raise ValueError("修订未能解析出改进回答")

        original = qa.answer or base_analysis.original_answer or ""
        md = _assemble_analysis_markdown(
            assessment=assessment,
            original=original,
            issues=issues,
            improved=improved,
        )
        resume_refs = re.findall(
            r"「([^」]{2,40})」|【([^】]{2,40})】",
            improved + "\n" + "\n".join(issues),
        )
        return AnalysisResult(
            qa_id=qa.id,
            assessment_point=assessment,
            original_answer=original,
            issues=issues,
            improved_answer=improved,
            resume_refs=[a or b for a, b in resume_refs],
            position=pos,
            raw_markdown=md,
            success=True,
            error=None,
            user_feedback=feedback,
            based_on_version=int(base_analysis.version or 1),
            version=1,
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as e:
        logger.error(with_ctx("单题修订失败 qa_id=%s: %s"), qa.id, e)
        raise
