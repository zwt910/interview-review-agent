"""API 路由：会话管理 + SSE 流式生成。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.models.session import SessionCreateResponse, SessionStatus
from src.parsers.docx_parser import parse_docx
from src.parsers.exceptions import ParseError
from src.pipeline.analysis_control import get_controller
from src.pipeline.events import event_to_sse
from src.pipeline.review_pipeline import InterviewReviewPipeline
from src.pipeline.wizard import ParseWizard
from src.storage.session_store import ARTIFACT_FILES, get_session_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["sessions"])


async def _read_upload(f: Optional[UploadFile]) -> tuple[Optional[bytes], Optional[str]]:
    if f is None:
        return None, None
    data = await f.read()
    return data, f.filename or "upload.bin"


class RenameBody(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


class TextConfirmBody(BaseModel):
    content: str = Field(..., min_length=1)


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
async def wizard_analyze_stream(session_id: str, body: TextConfirmBody):
    """确认 QA 后 SSE 逐题分析（提交可编辑后的 interview_qa.md）。"""
    store = get_session_store()
    if not store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")

    pipeline = InterviewReviewPipeline()

    async def event_gen():
        async for ev in pipeline.analyze_session_stream(
            session_id, interview_qa_md=body.content
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


@router.post("/sessions/{session_id}/wizard/analyze/pause")
async def wizard_analyze_pause(session_id: str):
    """暂停逐题分析：不再启动新题；进行中的题仍会跑完并落盘。"""
    store = get_session_store()
    if not store.exists(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    ctrl = get_controller(session_id)
    if ctrl is None:
        raise HTTPException(status_code=409, detail="当前没有进行中的分析任务")
    changed = ctrl.pause()
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
    ctrl = get_controller(session_id)
    if ctrl is None:
        raise HTTPException(
            status_code=409,
            detail="当前没有可继续的分析任务，请重新点击「开始逐题分析」",
        )
    changed = ctrl.resume()
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
