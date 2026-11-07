"""
端到端复盘流水线：FR1 → FR6。

支持 SSE 事件流：progress / session / qa_done / done / error。
旧会话数据不删除；新字段增量写入。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Optional

from config.llm_config import get_settings
from src.llm.client import get_llm_client_for
from src.models.schemas import AnalysisResult, PipelineResult, ResumeSummary
from src.models.session import SessionMeta, SessionStatus, default_session_title
from src.parsers.dispatcher import extract_text_from_file
from src.parsers.exceptions import ParseError
from src.pipeline.analysis_control import acquire_controller, release_controller
from src.pipeline.events import (
    DoneEvent,
    ErrorEvent,
    PausedEvent,
    PipelineEvent,
    ProgressEvent,
    QADoneEvent,
    ResumedEvent,
    SessionReadyEvent,
)
from src.services.dialogue_organize import organize_dialogue
from src.services.qa_analyzer import _analyze_one
from src.services.qa_splitter import split_qa_markdown
from src.services.report_generator import generate_review_markdown
from src.services.context_compress import load_or_build_analysis_context
from src.services.resume_summary import summarize_resume
from src.storage.resume_notes_history import get_resume_notes_history_store
from src.storage.session_store import SessionStore, get_session_store
from src.utils.request_context import set_session_id, with_ctx
from src.utils.resume_notes import normalize_resume_notes

logger = logging.getLogger(__name__)


class InterviewReviewPipeline:
    """面试复盘主流程。"""

    def __init__(self, *, store: Optional[SessionStore] = None, output_dir: Optional[Path] = None):
        self.settings = get_settings()
        self.store = store or get_session_store()
        self.output_dir = Path(output_dir or self.settings.output_dir)

    async def run_stream(
        self,
        *,
        dialogue_bytes: bytes,
        dialogue_filename: str,
        position: str,
        resume_bytes: Optional[bytes] = None,
        resume_filename: Optional[str] = None,
        job_description: str = "",
        resume_notes: str = "",
        title: Optional[str] = None,
        job_id: Optional[str] = None,
        persist: Optional[bool] = None,
        save_session: bool = True,
    ) -> AsyncIterator[PipelineEvent]:
        """异步事件流执行流水线。"""
        session_id = job_id or uuid.uuid4().hex[:12]
        warnings: list[str] = []
        position = (position or "").strip() or "待确认"
        jd_text = (job_description or "").strip()
        notes = normalize_resume_notes(resume_notes)
        t0 = time.perf_counter()
        session_meta: Optional[SessionMeta] = None

        try:
            if save_session:
                session_meta = self.store.create(
                    session_id=session_id,
                    title=title,
                    dialogue_filename=dialogue_filename,
                    resume_filename=resume_filename,
                    position=position,
                    has_job_description=bool(jd_text),
                    has_resume_notes=bool(notes),
                )
                self.store.save_upload(
                    session_id,
                    filename=dialogue_filename,
                    data=dialogue_bytes,
                    kind="dialogue",
                )
                if resume_bytes and resume_filename:
                    self.store.save_upload(
                        session_id,
                        filename=resume_filename,
                        data=resume_bytes,
                        kind="resume",
                    )
                if jd_text:
                    self.store.save_text_artifact(session_id, "job_description.txt", jd_text)
                if notes:
                    self.store.save_text_artifact(session_id, "resume_notes.txt", notes)
                    try:
                        get_resume_notes_history_store().add(notes)
                    except Exception:
                        logger.exception(
                            with_ctx(
                                "写入简历注释历史失败（不影响主流程）",
                                session_id=session_id,
                            )
                        )
                self.store.mark_running(session_id)

            yield SessionReadyEvent(
                session_id=session_id,
                title=(session_meta.title if session_meta else title)
                or default_session_title(position=position, dialogue_filename=dialogue_filename),
                position=position,
            )

            # ---- 1/4 简历解析 ----
            yield ProgressEvent(
                step=1,
                label="正在进行简历解析",
                session_id=session_id,
                skipped=not bool(resume_bytes and resume_filename),
            )

            resume_summary: Optional[ResumeSummary] = None
            resume_text = ""
            if resume_bytes and resume_filename:
                try:
                    resume_parse = extract_text_from_file(
                        resume_bytes, resume_filename, kind="resume"
                    )
                    warnings.extend(resume_parse.warnings)
                    resume_text = resume_parse.text
                    if save_session:
                        self.store.save_text_artifact(session_id, "resume_raw.txt", resume_text)
                    resume_summary = summarize_resume(
                        resume_text,
                        resume_notes=notes,
                        client=get_llm_client_for("resume"),
                    )
                    if resume_summary.is_fallback_text:
                        warnings.append("简历结构化摘要失败，已降级为纯文本摘要")
                    if save_session and resume_summary is not None:
                        self.store.save_json_artifact(
                            session_id,
                            "resume_summary.json",
                            resume_summary.model_dump(),
                        )
                except ParseError as e:
                    warnings.append(f"简历解析失败，将仅基于对话分析：{e.message}")
                    warnings.append("无简历参照，改进回答受限")
            else:
                warnings.append("未上传简历：无简历参照，改进回答受限")

            # ---- 2/4 对话整理 ----
            yield ProgressEvent(step=2, label="正在进行对话整理", session_id=session_id)

            try:
                dialogue_parse = extract_text_from_file(
                    dialogue_bytes, dialogue_filename, kind="dialogue"
                )
            except ParseError as e:
                raise ValueError(f"对话解析失败：{e.message}") from e

            warnings.extend(dialogue_parse.warnings)
            dialogue_text = dialogue_parse.text
            if save_session:
                self.store.save_text_artifact(session_id, "dialogue_raw.txt", dialogue_text)

            interview_qa_md = organize_dialogue(
                dialogue_text, client=get_llm_client_for("dialogue")
            )
            if save_session:
                self.store.save_text_artifact(session_id, "interview_qa.md", interview_qa_md)

            qa_items = split_qa_markdown(interview_qa_md)
            for qa in qa_items:
                warnings.extend(f"{qa.id}: {w}" for w in qa.flags.warnings)
            if save_session:
                self.store.save_json_artifact(
                    session_id, "qa_items.json", [q.model_dump() for q in qa_items]
                )
                self.store.update_meta_fields(session_id, qa_count=len(qa_items))

            # ---- 3/4 逐题分析（完成一题推一题）----
            yield ProgressEvent(step=3, label="正在压缩分析上下文", session_id=session_id)
            ctx = load_or_build_analysis_context(
                self.store,
                session_id,
                resume_summary=resume_summary,
                job_description=jd_text,
                resume_notes=notes,
                position=position,
            )
            yield ProgressEvent(step=3, label="正在逐题分析", session_id=session_id)

            llm = get_llm_client_for("analysis")
            semaphore = asyncio.Semaphore(self.settings.qa_analysis_concurrency)
            total = len(qa_items)
            results_slot: list[Optional[AnalysisResult]] = [None] * total
            completed = 0
            lock = asyncio.Lock()
            pending_events: asyncio.Queue[QADoneEvent] = asyncio.Queue()

            async def _run(idx: int, qa) -> None:
                nonlocal completed
                analysis = await _analyze_one(
                    qa,
                    resume_summary_json=ctx.resume_json,
                    position=position,
                    job_description=ctx.job_description,
                    resume_notes=ctx.resume_notes,
                    client=llm,
                    semaphore=semaphore,
                )
                results_slot[idx] = analysis
                if save_session:
                    self.store.append_analysis(session_id, analysis)
                async with lock:
                    completed += 1
                    done_count = completed
                await pending_events.put(
                    QADoneEvent(
                        session_id=session_id,
                        index=idx,
                        completed_count=done_count,
                        total_count=total,
                        qa=qa,
                        analysis=analysis,
                    )
                )

            tasks = [asyncio.create_task(_run(i, qa)) for i, qa in enumerate(qa_items)]
            finished = 0
            while finished < total:
                ev = await pending_events.get()
                finished += 1
                yield ev
            await asyncio.gather(*tasks)
            analyses = [r for r in results_slot if r is not None]

            # ---- 4/4 合并结果 ----
            yield ProgressEvent(step=4, label="正在合并结果", session_id=session_id)

            review_md = generate_review_markdown(
                qa_items=qa_items,
                analyses=analyses,
                resume_summary=resume_summary,
                position=position,
                extra_warnings=warnings,
            )

            result = PipelineResult(
                job_id=session_id,
                position=position,
                resume_summary=resume_summary,
                qa_items=qa_items,
                analyses=analyses,
                interview_qa_md=interview_qa_md,
                interview_review_md=review_md,
                warnings=warnings,
                artifact_paths={},
            )

            final_title = title or default_session_title(
                position=position, dialogue_filename=dialogue_filename
            )
            if save_session:
                self.store.save_pipeline_result(
                    session_id,
                    result,
                    title=final_title,
                    job_description=jd_text,
                )

            logger.info(
                "流水线完成 session=%s qa=%s elapsed=%.2fs",
                session_id,
                len(qa_items),
                time.perf_counter() - t0,
            )
            yield DoneEvent(
                session_id=session_id,
                title=final_title,
                position=position,
                qa_count=len(qa_items),
                analysis_success_count=sum(1 for a in analyses if a.success),
                warnings=warnings,
                interview_review_md=review_md,
                links={
                    "session": f"/api/v1/sessions/{session_id}",
                    "review_md": f"/api/v1/sessions/{session_id}/artifacts/interview_review.md",
                    "qa_md": f"/api/v1/sessions/{session_id}/artifacts/interview_qa.md",
                },
            )
        except Exception as e:
            logger.exception("流水线失败 session=%s", session_id)
            if save_session and self.store.exists(session_id):
                self.store.mark_failed(session_id, str(e))
            yield ErrorEvent(session_id=session_id, message=str(e))

    async def analyze_session_stream(
        self,
        session_id: str,
        *,
        interview_qa_md: Optional[str] = None,
    ) -> AsyncIterator[PipelineEvent]:
        """需求3：用户确认 QA 后，仅跑切分 + 逐题分析 + 报告（SSE）。

        需求4：支持暂停（不再启动新题）/ 继续（从剩余题接着分析）。
        """
        t0 = time.perf_counter()
        controller = None
        workers: list[asyncio.Task] = []
        stream_outcome = "unknown"
        set_session_id(session_id)
        try:
            if not self.store.exists(session_id):
                raise FileNotFoundError(f"会话不存在: {session_id}")
            detail = self.store.get_detail(session_id)
            meta = detail.meta
            position = meta.position or "待确认"
            jd_text = detail.job_description or ""
            notes = normalize_resume_notes(detail.resume_notes)
            warnings = list(meta.warnings or [])
            resume_summary = detail.resume_summary

            qa_md = (interview_qa_md if interview_qa_md is not None else detail.interview_qa_md) or ""
            qa_md = qa_md.strip()
            if not qa_md:
                raise ValueError("问答 markdown 为空，请先完成解析确认")

            self.store.save_text_artifact(session_id, "interview_qa.md", qa_md)
            self.store.update_meta_fields(
                session_id,
                status=SessionStatus.ANALYZING,
                workflow_step="analyzing",
            )

            logger.info(
                with_ctx(
                    "向导分析开始 qa_md_chars=%s has_resume=%s has_jd=%s jd_chars=%s "
                    "has_notes=%s notes_chars=%s position=%r",
                    session_id=session_id,
                ),
                len(qa_md),
                resume_summary is not None,
                bool(jd_text),
                len(jd_text),
                bool(notes),
                len(notes),
                position,
            )

            yield SessionReadyEvent(
                session_id=session_id,
                title=meta.title,
                position=position,
            )
            yield ProgressEvent(step=3, label="正在压缩分析上下文", session_id=session_id)
            ctx = load_or_build_analysis_context(
                self.store,
                session_id,
                resume_summary=resume_summary,
                job_description=jd_text,
                resume_notes=notes,
                position=position,
            )
            yield ProgressEvent(step=3, label="正在逐题分析", session_id=session_id)

            qa_items = split_qa_markdown(qa_md)
            for qa in qa_items:
                warnings.extend(f"{qa.id}: {w}" for w in qa.flags.warnings)
            self.store.save_json_artifact(
                session_id, "qa_items.json", [q.model_dump() for q in qa_items]
            )
            self.store.update_meta_fields(session_id, qa_count=len(qa_items))

            llm = get_llm_client_for("analysis")
            conc = max(1, int(self.settings.qa_analysis_concurrency))
            total = len(qa_items)

            # 已有分析结果（断线重连 / 继续）跳过，避免重复调用
            existing = list(detail.analyses or [])
            done_ids = {a.qa_id for a in existing}
            results_by_id: dict[str, AnalysisResult] = {a.qa_id: a for a in existing}
            pending_indices = [i for i, qa in enumerate(qa_items) if qa.id not in done_ids]
            completed = len(done_ids)
            remaining = len(pending_indices)

            if done_ids:
                logger.info(
                    with_ctx(
                        "跳过已完成题目 skip=%s pending=%s total=%s ids=%s",
                        session_id=session_id,
                    ),
                    len(done_ids),
                    remaining,
                    total,
                    ",".join(sorted(done_ids)),
                )

            # 把已完成题推给前端（重连场景）
            for i, qa in enumerate(qa_items):
                if qa.id in results_by_id:
                    yield QADoneEvent(
                        session_id=session_id,
                        index=i,
                        completed_count=completed,
                        total_count=total,
                        qa=qa,
                        analysis=results_by_id[qa.id],
                    )

            if remaining == 0:
                logger.info(
                    with_ctx("无需新分析，直接合并报告 total=%s", session_id=session_id),
                    total,
                )
                analyses = [results_by_id[q.id] for q in qa_items if q.id in results_by_id]
            else:
                controller = await acquire_controller(session_id)
                pending_list = list(pending_indices)
                claim_lock = asyncio.Lock()
                completed_lock = asyncio.Lock()
                slot_sem = asyncio.Semaphore(conc)
                logger.info(
                    with_ctx(
                        "启动分析 worker concurrency=%s pending=%s",
                        session_id=session_id,
                    ),
                    min(conc, remaining),
                    remaining,
                )

                async def _worker() -> None:
                    nonlocal completed
                    while not controller.is_closed:
                        await controller.wait_while_paused()
                        if controller.is_closed:
                            return
                        async with claim_lock:
                            if not pending_list:
                                return
                            if controller.is_paused:
                                continue
                            idx = pending_list.pop(0)

                        qa = qa_items[idx]
                        t_qa = time.perf_counter()
                        logger.info(
                            with_ctx(
                                "开始分析题目 index=%s qa_id=%s remaining_after_claim=%s",
                                session_id=session_id,
                            ),
                            idx,
                            qa.id,
                            len(pending_list),
                        )
                        analysis = await _analyze_one(
                            qa,
                            resume_summary_json=ctx.resume_json,
                            position=position,
                            job_description=ctx.job_description,
                            resume_notes=ctx.resume_notes,
                            client=llm,
                            semaphore=slot_sem,
                        )
                        if controller.is_closed:
                            logger.info(
                                with_ctx(
                                    "题目分析完成但控制器已关闭，丢弃落盘 index=%s qa_id=%s",
                                    session_id=session_id,
                                ),
                                idx,
                                qa.id,
                            )
                            return
                        results_by_id[qa.id] = analysis
                        self.store.append_analysis(session_id, analysis)
                        async with completed_lock:
                            completed += 1
                            done_count = completed
                        logger.info(
                            with_ctx(
                                "题目分析完成 index=%s qa_id=%s success=%s "
                                "progress=%s/%s elapsed=%.2fs",
                                session_id=session_id,
                            ),
                            idx,
                            qa.id,
                            analysis.success,
                            done_count,
                            total,
                            time.perf_counter() - t_qa,
                        )
                        await controller.bridge.put(
                            (
                                "qa",
                                QADoneEvent(
                                    session_id=session_id,
                                    index=idx,
                                    completed_count=done_count,
                                    total_count=total,
                                    qa=qa,
                                    analysis=analysis,
                                ),
                            )
                        )

                workers = [
                    asyncio.create_task(_worker(), name=f"analyze-{session_id}-{n}")
                    for n in range(min(conc, remaining))
                ]

                finished_new = 0
                while finished_new < remaining:
                    kind, payload = await controller.bridge.get()
                    if kind == "paused":
                        self.store.update_meta_fields(
                            session_id,
                            status=SessionStatus.PAUSED,
                            workflow_step="paused",
                        )
                        logger.info(
                            with_ctx(
                                "分析流收到暂停 progress=%s/%s pending_left=%s",
                                session_id=session_id,
                            ),
                            completed,
                            total,
                            len(pending_list),
                        )
                        yield PausedEvent(
                            session_id=session_id,
                            completed_count=completed,
                            total_count=total,
                        )
                        continue
                    if kind == "resumed":
                        self.store.update_meta_fields(
                            session_id,
                            status=SessionStatus.ANALYZING,
                            workflow_step="analyzing",
                        )
                        logger.info(
                            with_ctx(
                                "分析流收到继续 progress=%s/%s pending_left=%s",
                                session_id=session_id,
                            ),
                            completed,
                            total,
                            len(pending_list),
                        )
                        yield ResumedEvent(
                            session_id=session_id,
                            completed_count=completed,
                            total_count=total,
                        )
                        continue
                    if kind == "qa":
                        finished_new += 1
                        yield payload

                await asyncio.gather(*workers, return_exceptions=True)
                analyses = [results_by_id[q.id] for q in qa_items if q.id in results_by_id]

            yield ProgressEvent(step=4, label="正在合并结果", session_id=session_id)
            logger.info(with_ctx("开始合并长篇报告", session_id=session_id))
            review_md = generate_review_markdown(
                qa_items=qa_items,
                analyses=analyses,
                resume_summary=resume_summary,
                position=position,
                extra_warnings=warnings,
            )
            result = PipelineResult(
                job_id=session_id,
                position=position,
                resume_summary=resume_summary,
                qa_items=qa_items,
                analyses=analyses,
                interview_qa_md=qa_md,
                interview_review_md=review_md,
                warnings=warnings,
                artifact_paths={},
            )
            self.store.save_pipeline_result(
                session_id,
                result,
                title=meta.title,
                job_description=jd_text,
            )
            self.store.update_meta_fields(session_id, workflow_step="done")

            success_n = sum(1 for a in analyses if a.success)
            logger.info(
                with_ctx(
                    "向导分析完成 qa=%s success=%s review_chars=%s elapsed=%.2fs",
                    session_id=session_id,
                ),
                len(qa_items),
                success_n,
                len(review_md or ""),
                time.perf_counter() - t0,
            )
            stream_outcome = "completed"
            yield DoneEvent(
                session_id=session_id,
                title=meta.title,
                position=position,
                qa_count=len(qa_items),
                analysis_success_count=success_n,
                warnings=warnings,
                interview_review_md=review_md,
                links={
                    "session": f"/api/v1/sessions/{session_id}",
                    "review_md": f"/api/v1/sessions/{session_id}/artifacts/interview_review.md",
                    "qa_md": f"/api/v1/sessions/{session_id}/artifacts/interview_qa.md",
                },
            )
        except asyncio.CancelledError:
            stream_outcome = "cancelled"
            logger.warning(
                with_ctx(
                    "向导分析流被取消（多为客户端断开 SSE）elapsed=%.2fs",
                    session_id=session_id,
                ),
                time.perf_counter() - t0,
            )
            raise
        except Exception as e:
            stream_outcome = "error"
            logger.exception(
                with_ctx("向导分析失败 err=%s", session_id=session_id),
                e,
            )
            if self.store.exists(session_id):
                self.store.mark_failed(session_id, str(e))
            yield ErrorEvent(session_id=session_id, message=str(e))
        finally:
            logger.info(
                with_ctx(
                    "向导分析流结束 outcome=%s elapsed=%.2fs",
                    session_id=session_id,
                ),
                stream_outcome,
                time.perf_counter() - t0,
            )
            for t in workers:
                if not t.done():
                    t.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            await release_controller(session_id)

    async def run_async(self, **kwargs) -> tuple[PipelineResult, Optional[SessionMeta]]:
        """兼容旧入口：消费流至 done。"""
        last_done: Optional[DoneEvent] = None
        session_id = ""
        async for ev in self.run_stream(**kwargs):
            if isinstance(ev, SessionReadyEvent):
                session_id = ev.session_id
            elif isinstance(ev, ErrorEvent):
                raise RuntimeError(ev.message)
            elif isinstance(ev, DoneEvent):
                last_done = ev
                session_id = ev.session_id
        if not last_done or not session_id:
            raise RuntimeError("流水线未产生结果")
        detail = self.store.get_detail(session_id)
        result = PipelineResult(
            job_id=session_id,
            position=last_done.position,
            resume_summary=detail.resume_summary,
            qa_items=detail.qa_items,
            analyses=detail.analyses,
            interview_qa_md=detail.interview_qa_md,
            interview_review_md=detail.interview_review_md,
            warnings=last_done.warnings,
        )
        return result, detail.meta

    def run(self, **kwargs) -> tuple[PipelineResult, Optional[SessionMeta]]:
        return asyncio.run(self.run_async(**kwargs))
