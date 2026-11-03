"""API 路由：会话管理 + SSE 流式生成。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.models.session import SessionCreateResponse, SessionStatus
from src.parsers.docx_parser import parse_docx
from src.parsers.exceptions import ParseError
from src.pipeline.analysis_control import get_controller
from src.pipeline.events import event_to_sse
from src.pipeline.review_pipeline import InterviewReviewPipeline
from src.pipeline.wizard import ParseWizard
from src.services.qa_analyzer import revise_one
from src.services.report_generator import generate_review_markdown
from src.storage.resume_notes_history import get_resume_notes_history_store
from src.storage.session_store import ARTIFACT_FILES, get_session_store
from src.utils.request_context import get_request_id, set_request_id, set_session_id, with_ctx

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["sessions"])


async def _read_upload(f: Optional[UploadFile]) -> tuple[Optional[bytes], Optional[str]]:
    if f is None:
        return None, None
    data = await f.read()
    return data, f.filename or "upload.bin"


def _assert_session_revisable(status: SessionStatus | str) -> None:
    st = status.value if isinstance(status, SessionStatus) else str(status)
    if st not in (SessionStatus.COMPLETED.value, SessionStatus.PAUSED.value):
        raise HTTPException(
            status_code=409,
            detail="仅在分析已完成或已暂停时可修改/删除版本",
        )


def _regenerate_review_md(session_id: str) -> str:
    store = get_session_store()
    detail = store.get_detail(session_id)
    review_md = generate_review_markdown(
        qa_items=detail.qa_items,
        analyses=detail.analyses,
        resume_summary=detail.resume_summary,
        position=detail.meta.position or "待确认",
        extra_warnings=list(detail.meta.warnings or []),
    )
    store.save_review_markdown(session_id, review_md)
    return review_md


class RenameBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class TextConfirmBody(BaseModel):
    content: str = Field(..., min_length=1)


class ReviseBody(BaseModel):
    feedback: str = Field(..., min_length=1, max_length=8000)
    base_version: int = Field(..., ge=1, description="当前查看的版本号")


@router.get("/resume-notes/history")
async def list_resume_notes_history(limit: int = 30):
    """简历注释历史列表（供首页一键回填）。"""
    store = get_resume_notes_history_store()
    items = store.list_items(limit=limit)
    return {"total": len(items), "items": items}


@router.delete("/resume-notes/history/{item_id}")
async def delete_resume_notes_history(item_id: str):
    store = get_resume_notes_history_store()
    ok = store.delete(item_id)
    if not ok:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    return {"ok": True, "id": item_id}


@router.get("/sessions")
async def list_sessions(limit: int = 50, offset: int = 0):
    store = get_session_store()
    items = store.list_sessions(limit=limit, offset=offset)
    return {
        "total_returned": len(items),
        "sessions": [m.model_dump() for m in items],
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    store = get_session_store()
    try:
        detail = store.get_detail(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return detail.model_dump()


@router.get("/sessions/{session_id}/meta")
async def get_session_meta(session_id: str):
    store = get_session_store()
    try:
        meta = store.get_meta(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return meta.model_dump()


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, body: RenameBody):
    store = get_session_store()
    try:
        meta = store.rename(session_id, body.title)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return meta.model_dump()


@router.post("/sessions/{session_id}/analyses/{qa_id}/revise")
async def revise_analysis(session_id: str, qa_id: str, body: ReviseBody):
    """按用户要求基于指定版本重生成单题分析，并追加新版本。"""
    store = get_session_store()
    if not store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    set_session_id(session_id)

    detail = store.get_detail(session_id)
    _assert_session_revisable(detail.meta.status)

    qa = next((q for q in detail.qa_items if q.id == qa_id), None)
    if qa is None:
        raise HTTPException(status_code=404, detail=f"题目不存在: {qa_id}")

    versions = store.list_analyses_for_qa(session_id, qa_id)
    base = next((a for a in versions if int(a.version or 1) == int(body.base_version)), None)
    if base is None:
        raise HTTPException(
            status_code=404,
            detail=f"基准版本不存在: {qa_id} V{body.base_version}",
        )

    feedback = body.feedback.strip()
    logger.info(
        with_ctx(
            "修订接口 qa_id=%s base_version=%s feedback_chars=%s",
            session_id=session_id,
        ),
        qa_id,
        body.base_version,
        len(feedback),
    )
    try:
        draft = await revise_one(
            qa,
            base_analysis=base,
            user_feedback=feedback,
            resume_summary=detail.resume_summary,
            position=detail.meta.position or "待确认",
            job_description=detail.job_description or "",
            resume_notes=detail.resume_notes or "",
        )
        saved = store.append_analysis_version(
            session_id,
            draft,
            based_on_version=int(body.base_version),
            user_feedback=feedback,
        )
        review_md = _regenerate_review_md(session_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception(with_ctx("修订失败", session_id=session_id))
        raise HTTPException(status_code=500, detail=f"修订失败：{e}") from e

    logger.info(
        with_ctx(
            "修订完成 qa_id=%s new_version=%s",
            session_id=session_id,
        ),
        qa_id,
        saved.version,
    )
    return {
        "session_id": session_id,
        "qa_id": qa_id,
        "analysis": saved.model_dump(),
        "versions": [a.model_dump() for a in store.list_analyses_for_qa(session_id, qa_id)],
        "interview_review_md": review_md,
    }


@router.delete("/sessions/{session_id}/analyses/{qa_id}/versions/{version}")
async def delete_analysis_version(session_id: str, qa_id: str, version: int):
    """删除单题某一分析版本（仅剩一版时不可删）。"""
    store = get_session_store()
    if not store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    set_session_id(session_id)
    detail = store.get_detail(session_id)
    _assert_session_revisable(detail.meta.status)

    try:
        remaining_all = store.delete_analysis_version(session_id, qa_id, version)
        review_md = _regenerate_review_md(session_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception(with_ctx("删除版本失败", session_id=session_id))
        raise HTTPException(status_code=500, detail=f"删除失败：{e}") from e

    versions = [a for a in remaining_all if a.qa_id == qa_id]
    versions.sort(key=lambda a: a.version)
    latest = versions[-1].version if versions else None
    logger.info(
        with_ctx(
            "已删除版本 qa_id=%s version=%s remaining=%s",
            session_id=session_id,
        ),
        qa_id,
        version,
        len(versions),
    )
    return {
        "session_id": session_id,
        "qa_id": qa_id,
        "deleted_version": version,
        "versions": [a.model_dump() for a in versions],
        "latest_version": latest,
        "interview_review_md": review_md,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    store = get_session_store()
    try:
        store.delete(session_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    return {"ok": True, "session_id": session_id}


@router.get("/sessions/{session_id}/artifacts/{name}")
async def get_session_artifact(session_id: str, name: str):
    store = get_session_store()
    try:
        path = store.get_artifact_path(session_id, name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    if name.endswith(".md"):
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=name)
    if name.endswith(".json"):
        return FileResponse(path, media_type="application/json; charset=utf-8", filename=name)
    return PlainTextResponse(path.read_text(encoding="utf-8"), media_type="text/plain; charset=utf-8")


async def _resolve_job_description(
    jd_text: Optional[str],
    jd_file: Optional[UploadFile],
) -> str:
    text = (jd_text or "").strip()
    file_bytes, file_name = await _read_upload(jd_file)
    if file_bytes and file_name:
        suffix = Path(file_name).suffix.lower()
        if suffix != ".docx":
            raise HTTPException(status_code=400, detail="岗位 JD 文件仅支持 .docx")
        try:
            parsed = parse_docx(file_bytes, source_type="jd_docx", filename=file_name)
            file_text = parsed.text.strip()
        except ParseError as e:
            raise HTTPException(status_code=422, detail=f"岗位 JD 解析失败：{e.message}") from e
        # 文件优先；若同时填了文本则拼接（文件在前）
        if text:
            return f"{file_text}\n\n{text}".strip()
        return file_text
    return text


# =====================================================================
# 需求3：文稿解析向导（转写 → 问题 → QA → 分析）
# =====================================================================


@router.post("/sessions/wizard/start")
async def wizard_start(
    dialogue: UploadFile = File(..., description="面试对话 .docx"),
    position: str = Form(..., description="面试岗位（必填）"),
    resume: Optional[UploadFile] = File(None),
    resume_notes: Optional[str] = Form(None, description="简历注释（可选纯文本）"),
    job_description: Optional[str] = Form(None),
    jd_file: Optional[UploadFile] = File(None),
    title: Optional[str] = Form(None),
):
    """上传文件 → 简历摘要 + 文稿转写，返回可编辑转写稿。"""
    pos = (position or "").strip()
    if not pos:
        raise HTTPException(status_code=400, detail="面试岗位为必填项")

    dialogue_bytes, dialogue_name = await _read_upload(dialogue)
    if not dialogue_bytes:
        raise HTTPException(status_code=400, detail="对话文件为空")
    if Path(dialogue_name or "").suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="对话文档仅支持 .docx")

    resume_bytes, resume_name = await _read_upload(resume)
    if resume_name and Path(resume_name).suffix.lower() not in (".pdf", ".docx"):
        raise HTTPException(status_code=400, detail="简历仅支持 .pdf / .docx")

    jd_text = await _resolve_job_description(job_description, jd_file)
    wizard = ParseWizard()
    try:
        return wizard.start(
            dialogue_bytes=dialogue_bytes,
            dialogue_filename=dialogue_name or "dialogue.docx",
            position=pos,
            resume_bytes=resume_bytes,
            resume_filename=resume_name,
            job_description=jd_text,
            resume_notes=resume_notes or "",
            title=title,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("向导启动失败")
        raise HTTPException(status_code=500, detail=f"内部错误：{e}") from e


@router.post("/sessions/{session_id}/wizard/confirm-transcript")
async def wizard_confirm_transcript(session_id: str, body: TextConfirmBody):
    """确认转写稿 → 澄清问题列表。"""
    wizard = ParseWizard()
    try:
        return wizard.confirm_transcript(session_id, body.content)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("澄清问题失败")
        raise HTTPException(status_code=500, detail=f"内部错误：{e}") from e


@router.post("/sessions/{session_id}/wizard/confirm-questions")
async def wizard_confirm_questions(session_id: str, body: TextConfirmBody):
    """确认问题列表 → 生成 QA markdown。"""
    wizard = ParseWizard()
    try:
        return wizard.confirm_questions(session_id, body.content)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="会话不存在") from None
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("生成 QA 失败")
        raise HTTPException(status_code=500, detail=f"内部错误：{e}") from e


@router.post("/sessions/{session_id}/wizard/analyze/stream")
async def wizard_analyze_stream(
    session_id: str, body: TextConfirmBody, request: Request
):
    """确认 QA 后 SSE 逐题分析（提交可编辑后的 interview_qa.md）。"""
    store = get_session_store()
    if not store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    pipeline = InterviewReviewPipeline()
    # SSE 生命周期可能长于中间件；捕获 req_id 后在生成器内重新绑定
    req_id = getattr(request.state, "request_id", None) or get_request_id()
    qa_chars = len((body.content or "").strip())
    logger.info(
        with_ctx(
            "SSE 分析请求接入 qa_chars=%s",
            session_id=session_id,
        ),
        qa_chars,
    )

    async def event_gen():
        set_request_id(req_id)
        set_session_id(session_id)
        t0 = time.perf_counter()
        outcome = "completed"
        agen = pipeline.analyze_session_stream(
            session_id, interview_qa_md=body.content
        )
        try:
            async for ev in agen:
                if await request.is_disconnected():
                    outcome = "client_disconnected"
                    logger.warning(
                        with_ctx(
                            "检测到客户端已断开，停止推送 SSE",
                            session_id=session_id,
                        )
                    )
                    break
                yield event_to_sse(ev)
        except asyncio.CancelledError:
            outcome = "cancelled"
            logger.warning(
                with_ctx(
                    "SSE 生成器 CancelledError（客户端断开或服务取消）",
                    session_id=session_id,
                )
            )
            raise
        except Exception:
            outcome = "error"
            logger.exception(
                with_ctx("SSE 生成器异常", session_id=session_id)
            )
            raise
        finally:
            try:
                await agen.aclose()
            except Exception:
                logger.debug(
                    with_ctx("关闭分析异步生成器时忽略异常", session_id=session_id),
                    exc_info=True,
                )
            logger.info(
                with_ctx(
                    "SSE 分析请求结束 outcome=%s elapsed=%.2fs",
                    session_id=session_id,
                ),
                outcome,
                time.perf_counter() - t0,
            )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/wizard/analyze/pause")
async def wizard_analyze_pause(session_id: str):
    """暂停逐题分析：不再启动新题；进行中的题仍会跑完并落盘。"""
    store = get_session_store()
    if not store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    set_session_id(session_id)
    meta = store.get_meta(session_id)
    ctrl = get_controller(session_id)
    if ctrl is None:
        logger.warning(
            with_ctx(
                "暂停失败：无进行中的分析任务 status=%s step=%s",
                session_id=session_id,
            ),
            meta.status,
            meta.workflow_step,
        )
        raise HTTPException(status_code=409, detail="当前没有进行中的分析任务")
    changed = ctrl.pause()
    logger.info(
        with_ctx(
            "暂停接口调用 changed=%s status=%s qa_count=%s analysis_success=%s",
            session_id=session_id,
        ),
        changed,
        meta.status,
        meta.qa_count,
        meta.analysis_success_count,
    )
    return {
        "session_id": session_id,
        "paused": True,
        "changed": changed,
        "message": "已暂停，将不再分析新题目" if changed else "已处于暂停状态",
    }


@router.post("/sessions/{session_id}/wizard/analyze/resume")
async def wizard_analyze_resume(session_id: str):
    """继续逐题分析：从尚未完成的题目接着跑。"""
    store = get_session_store()
    if not store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    set_session_id(session_id)
    meta = store.get_meta(session_id)
    ctrl = get_controller(session_id)
    if ctrl is None:
        logger.warning(
            with_ctx(
                "继续失败：无控制器 status=%s step=%s",
                session_id=session_id,
            ),
            meta.status,
            meta.workflow_step,
        )
        raise HTTPException(
            status_code=409,
            detail="当前没有可继续的分析任务，请重新点击「开始逐题分析」",
        )
    changed = ctrl.resume()
    logger.info(
        with_ctx(
            "继续接口调用 changed=%s status=%s qa_count=%s analysis_success=%s",
            session_id=session_id,
        ),
        changed,
        meta.status,
        meta.qa_count,
        meta.analysis_success_count,
    )
    return {
        "session_id": session_id,
        "paused": False,
        "changed": changed,
        "message": "已继续分析" if changed else "当前未处于暂停状态",
    }


@router.post("/sessions/stream")
async def create_session_stream(
    dialogue: UploadFile = File(..., description="面试对话 .docx"),
    position: str = Form(..., description="面试岗位（必填）"),
    resume: Optional[UploadFile] = File(None, description="简历 .pdf / .docx（可选）"),
    resume_notes: Optional[str] = Form(None, description="简历注释（可选纯文本）"),
    job_description: Optional[str] = Form(None, description="岗位 JD 纯文本（可选）"),
    jd_file: Optional[UploadFile] = File(None, description="岗位 JD .docx（可选）"),
    title: Optional[str] = Form(None, description="会话标题（可选）"),
    persist: bool = Form(True),
):
    """SSE 流式生成：progress / session / qa_done / done / error。"""
    pos = (position or "").strip()
    if not pos:
        raise HTTPException(status_code=400, detail="面试岗位为必填项")

    dialogue_bytes, dialogue_name = await _read_upload(dialogue)
    if not dialogue_bytes:
        raise HTTPException(status_code=400, detail="对话文件为空")
    if Path(dialogue_name or "").suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="对话文档仅支持 .docx")

    resume_bytes, resume_name = await _read_upload(resume)
    if resume_name:
        rs = Path(resume_name).suffix.lower()
        if rs not in (".pdf", ".docx"):
            raise HTTPException(status_code=400, detail="简历仅支持 .pdf / .docx")

    jd_text = await _resolve_job_description(job_description, jd_file)

    logger.info(
        "SSE 生成请求 dialogue=%s resume=%s position=%s jd_chars=%s notes=%s",
        dialogue_name,
        resume_name or "-",
        pos,
        len(jd_text),
        len((resume_notes or "").strip()),
    )

    pipeline = InterviewReviewPipeline()

    async def event_gen():
        async for ev in pipeline.run_stream(
            dialogue_bytes=dialogue_bytes,
            dialogue_filename=dialogue_name or "dialogue.docx",
            resume_bytes=resume_bytes,
            resume_filename=resume_name,
            position=pos,
            job_description=jd_text,
            resume_notes=resume_notes or "",
            title=title,
            persist=persist,
            save_session=True,
        ):
            yield event_to_sse(ev)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions", response_model=SessionCreateResponse)
@router.post("/review", response_model=SessionCreateResponse)
async def create_session_review(
    dialogue: UploadFile = File(..., description="面试对话 .docx"),
    position: str = Form(..., description="面试岗位（必填）"),
    resume: Optional[UploadFile] = File(None),
    resume_notes: Optional[str] = Form(None, description="简历注释（可选纯文本）"),
    job_description: Optional[str] = Form(None),
    jd_file: Optional[UploadFile] = File(None),
    title: Optional[str] = Form(None),
    persist: bool = Form(True),
):
    """兼容旧接口：阻塞至全部完成。"""
    pos = (position or "").strip()
    if not pos:
        raise HTTPException(status_code=400, detail="面试岗位为必填项")

    dialogue_bytes, dialogue_name = await _read_upload(dialogue)
    if not dialogue_bytes:
        raise HTTPException(status_code=400, detail="对话文件为空")
    if Path(dialogue_name or "").suffix.lower() != ".docx":
        raise HTTPException(status_code=400, detail="对话文档仅支持 .docx")

    resume_bytes, resume_name = await _read_upload(resume)
    if resume_name and Path(resume_name).suffix.lower() not in (".pdf", ".docx"):
        raise HTTPException(status_code=400, detail="简历仅支持 .pdf / .docx")

    jd_text = await _resolve_job_description(job_description, jd_file)
    pipeline = InterviewReviewPipeline()
    try:
        result, meta = await pipeline.run_async(
            dialogue_bytes=dialogue_bytes,
            dialogue_filename=dialogue_name or "dialogue.docx",
            resume_bytes=resume_bytes,
            resume_filename=resume_name,
            position=pos,
            job_description=jd_text,
            resume_notes=resume_notes or "",
            title=title,
            persist=persist,
            save_session=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("流水线执行失败")
        raise HTTPException(status_code=500, detail=f"内部错误：{e}") from e

    assert meta is not None
    sid = meta.session_id
    return SessionCreateResponse(
        session_id=sid,
        title=meta.title,
        status=SessionStatus.COMPLETED,
        position=result.position,
        qa_count=len(result.qa_items),
        analysis_success_count=sum(1 for a in result.analyses if a.success),
        warnings=result.warnings,
        links={
            "session": f"/api/v1/sessions/{sid}",
            "review_md": f"/api/v1/sessions/{sid}/artifacts/interview_review.md",
            "qa_md": f"/api/v1/sessions/{sid}/artifacts/interview_qa.md",
        },
    )


@router.get("/jobs/{job_id}/review")
async def download_review_compat(job_id: str):
    return await get_session_artifact(job_id, "interview_review.md")


@router.get("/jobs/{job_id}/qa")
async def download_qa_compat(job_id: str):
    return await get_session_artifact(job_id, "interview_qa.md")


@router.get("/jobs/{job_id}/artifacts/{name}")
async def get_artifact_compat(job_id: str, name: str):
    if name not in ARTIFACT_FILES:
        raise HTTPException(status_code=400, detail="不允许的文件名")
    return await get_session_artifact(job_id, name)
