"""
需求3：交互式文稿解析向导（上传 → 转写 → 澄清问题 → 生成 QA → 逐题分析）。
旧会话数据不删除。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from src.llm.client import get_llm_client_for
from src.models.schemas import ResumeSummary
from src.models.session import SessionMeta, SessionStatus, default_session_title
from src.parsers.dispatcher import extract_text_from_file
from src.parsers.exceptions import ParseError
from src.services.dialogue_steps import (
    build_qa_from_questions,
    clarify_questions,
    transcribe_dialogue,
)
from src.services.resume_summary import summarize_resume
from src.storage.resume_notes_history import get_resume_notes_history_store
from src.storage.session_store import SessionStore, get_session_store
from src.utils.request_context import set_session_id, with_ctx
from src.utils.resume_notes import normalize_resume_notes

logger = logging.getLogger(__name__)


class ParseWizard:
    def __init__(self, store: Optional[SessionStore] = None):
        self.store = store or get_session_store()

    def _load_resume_summary(self, session_id: str) -> Optional[ResumeSummary]:
        detail = self.store.get_detail(session_id)
        return detail.resume_summary

    def start(
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
    ) -> dict:
        """上传并完成：解析 + 简历摘要 + 文稿转写。返回可编辑转写稿。"""
        position = (position or "").strip()
        if not position:
            raise ValueError("面试岗位为必填项")

        session_id = uuid.uuid4().hex[:12]
        set_session_id(session_id)
        warnings: list[str] = []
        jd_text = (job_description or "").strip()
        notes = normalize_resume_notes(resume_notes)
        t0 = time.perf_counter()

        logger.info(
            with_ctx(
                "向导启动 begin position=%r dialogue=%s dialogue_bytes=%s "
                "has_resume=%s resume=%s has_jd=%s jd_chars=%s has_notes=%s notes_chars=%s",
                session_id=session_id,
            ),
            position,
            dialogue_filename,
            len(dialogue_bytes or b""),
            bool(resume_bytes and resume_filename),
            resume_filename or "-",
            bool(jd_text),
            len(jd_text),
            bool(notes),
            len(notes),
        )

        meta = self.store.create(
            session_id=session_id,
            title=title,
            dialogue_filename=dialogue_filename,
            resume_filename=resume_filename,
            position=position,
            has_job_description=bool(jd_text),
            has_resume_notes=bool(notes),
        )
        self.store.save_upload(
            session_id, filename=dialogue_filename, data=dialogue_bytes, kind="dialogue"
        )
        if resume_bytes and resume_filename:
            self.store.save_upload(
                session_id, filename=resume_filename, data=resume_bytes, kind="resume"
            )
        if jd_text:
            self.store.save_text_artifact(session_id, "job_description.txt", jd_text)
        if notes:
            self.store.save_text_artifact(session_id, "resume_notes.txt", notes)
            try:
                get_resume_notes_history_store().add(notes)
            except Exception:
                logger.exception(
                    with_ctx("写入简历注释历史失败（不影响主流程）", session_id=session_id)
                )

        try:
            dialogue_parse = extract_text_from_file(
                dialogue_bytes, dialogue_filename, kind="dialogue"
            )
        except ParseError as e:
            self.store.mark_failed(session_id, e.message)
            logger.exception(
                with_ctx("向导启动失败：对话解析 session=%s", session_id=session_id),
                session_id,
            )
            raise ValueError(f"对话解析失败：{e.message}") from e

        warnings.extend(dialogue_parse.warnings)
        dialogue_text = dialogue_parse.text
        self.store.save_text_artifact(session_id, "dialogue_raw.txt", dialogue_text)
        logger.info(
            with_ctx("对话解析完成 chars=%s warnings=%s", session_id=session_id),
            len(dialogue_text),
            len(dialogue_parse.warnings),
        )

        resume_summary: Optional[ResumeSummary] = None
        if resume_bytes and resume_filename:
            t_resume = time.perf_counter()
            try:
                resume_parse = extract_text_from_file(
                    resume_bytes, resume_filename, kind="resume"
                )
                warnings.extend(resume_parse.warnings)
                self.store.save_text_artifact(session_id, "resume_raw.txt", resume_parse.text)
                logger.info(
                    with_ctx(
                        "简历摘要开始 raw_chars=%s notes_chars=%s",
                        session_id=session_id,
                    ),
                    len(resume_parse.text),
                    len(notes),
                )
                resume_summary = summarize_resume(
                    resume_parse.text,
                    resume_notes=notes,
                    client=get_llm_client_for("resume"),
                )
                if resume_summary.is_fallback_text:
                    warnings.append("简历结构化摘要失败，已降级为纯文本摘要")
                self.store.save_json_artifact(
                    session_id, "resume_summary.json", resume_summary.model_dump()
                )
                logger.info(
                    with_ctx(
                        "简历摘要完成 fallback=%s elapsed=%.2fs",
                        session_id=session_id,
                    ),
                    resume_summary.is_fallback_text,
                    time.perf_counter() - t_resume,
                )
            except ParseError as e:
                warnings.append(f"简历解析失败：{e.message}")
                logger.warning(
                    with_ctx("简历解析失败：%s", session_id=session_id),
                    e.message,
                )
        else:
            warnings.append("未上传简历")
            if notes:
                logger.info(
                    with_ctx(
                        "未上传简历但提供了注释 notes_chars=%s（将在澄清/分析阶段附带）",
                        session_id=session_id,
                    ),
                    len(notes),
                )

        t_tx = time.perf_counter()
        logger.info(with_ctx("向导转写开始", session_id=session_id))
        try:
            transcript = transcribe_dialogue(
                dialogue_text, client=get_llm_client_for("dialogue")
            )
        except Exception:
            logger.exception(with_ctx("向导转写失败", session_id=session_id))
            raise
        self.store.save_text_artifact(session_id, "cleaned_transcript.txt", transcript)
        logger.info(
            with_ctx("向导转写成功 chars=%s elapsed=%.2fs", session_id=session_id),
            len(transcript),
            time.perf_counter() - t_tx,
        )

        final_title = title or default_session_title(
            position=position, dialogue_filename=dialogue_filename
        )
        self.store.update_meta_fields(
            session_id,
            status=SessionStatus.AWAITING_TRANSCRIPT,
            workflow_step="transcript",
            title=final_title,
            warnings=warnings,
        )

        logger.info(
            with_ctx(
                "向导启动完成 title=%r warnings=%s total_elapsed=%.2fs",
                session_id=session_id,
            ),
            final_title,
            len(warnings),
            time.perf_counter() - t0,
        )
        return {
            "session_id": session_id,
            "title": final_title,
            "position": position,
            "workflow_step": "transcript",
            "cleaned_transcript": transcript,
            "warnings": warnings,
            "message": "请核对转写文稿，可编辑后点击下一步",
        }

    def confirm_transcript(self, session_id: str, transcript_text: str) -> dict:
        """用户确认转写稿 → 澄清问题。"""
        set_session_id(session_id)
        text = (transcript_text or "").strip()
        if not text:
            raise ValueError("转写文稿不能为空")
        if not self.store.exists(session_id):
            raise FileNotFoundError(f"会话不存在: {session_id}")

        self.store.save_text_artifact(session_id, "cleaned_transcript.txt", text)
        resume_summary = self._load_resume_summary(session_id)
        detail = self.store.get_detail(session_id)
        notes = normalize_resume_notes(detail.resume_notes)

        t0 = time.perf_counter()
        logger.info(
            with_ctx(
                "向导澄清问题开始 transcript_chars=%s has_resume=%s notes_chars=%s",
                session_id=session_id,
            ),
            len(text),
            resume_summary is not None,
            len(notes),
        )
        try:
            questions = clarify_questions(
                text,
                resume_summary,
                resume_notes=notes,
                client=get_llm_client_for("dialogue"),
            )
        except Exception:
            logger.exception(with_ctx("向导澄清问题失败", session_id=session_id))
            raise
        self.store.save_text_artifact(session_id, "clarified_questions.md", questions)
        self.store.update_meta_fields(
            session_id,
            status=SessionStatus.AWAITING_QUESTIONS,
            workflow_step="questions",
        )
        logger.info(
            with_ctx(
                "向导澄清问题成功 chars=%s elapsed=%.2fs",
                session_id=session_id,
            ),
            len(questions),
            time.perf_counter() - t0,
        )
        return {
            "session_id": session_id,
            "workflow_step": "questions",
            "clarified_questions": questions,
            "message": "请核对澄清后的问题列表，可编辑后点击下一步",
        }

    def confirm_questions(self, session_id: str, questions_text: str) -> dict:
        """用户确认问题列表 → 生成 QA。"""
        set_session_id(session_id)
        qs = (questions_text or "").strip()
        if not qs:
            raise ValueError("问题列表不能为空")
        if not self.store.exists(session_id):
            raise FileNotFoundError(f"会话不存在: {session_id}")

        self.store.save_text_artifact(session_id, "clarified_questions.md", qs)
        detail = self.store.get_detail(session_id)
        transcript = detail.cleaned_transcript
        if not transcript.strip():
            raise ValueError("缺少转写文稿，请先完成上一步")

        t0 = time.perf_counter()
        logger.info(
            with_ctx(
                "向导生成QA开始 questions_chars=%s transcript_chars=%s",
                session_id=session_id,
            ),
            len(qs),
            len(transcript),
        )
        try:
            qa_md = build_qa_from_questions(
                qs, transcript, client=get_llm_client_for("dialogue")
            )
        except Exception:
            logger.exception(with_ctx("向导生成QA失败", session_id=session_id))
            raise
        self.store.save_text_artifact(session_id, "interview_qa.md", qa_md)
        self.store.update_meta_fields(
            session_id,
            status=SessionStatus.AWAITING_QA,
            workflow_step="qa",
        )
        logger.info(
            with_ctx(
                "向导生成QA成功 chars=%s elapsed=%.2fs",
                session_id=session_id,
            ),
            len(qa_md),
            time.perf_counter() - t0,
        )
        return {
            "session_id": session_id,
            "workflow_step": "qa",
            "interview_qa_md": qa_md,
            "message": "请核对问答对，可编辑后开始逐题分析",
        }

    def save_qa_draft(self, session_id: str, interview_qa_md: str) -> SessionMeta:
        md = (interview_qa_md or "").strip()
        if not md:
            raise ValueError("问答 markdown 不能为空")
        self.store.save_text_artifact(session_id, "interview_qa.md", md)
        return self.store.update_meta_fields(
            session_id,
            status=SessionStatus.AWAITING_QA,
            workflow_step="qa",
        )
