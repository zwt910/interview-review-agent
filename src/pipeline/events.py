"""流水线 SSE 事件模型。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from src.models.schemas import AnalysisResult, QAItem


class ProgressEvent(BaseModel):
    type: Literal["progress"] = "progress"
    step: int
    total: int = 4
    label: str
    skipped: bool = False
    session_id: str = ""


class SessionReadyEvent(BaseModel):
    type: Literal["session"] = "session"
    session_id: str
    title: str
    position: str


class QADoneEvent(BaseModel):
    type: Literal["qa_done"] = "qa_done"
    session_id: str
    index: int  # 0-based
    completed_count: int
    total_count: int
    qa: QAItem
    analysis: AnalysisResult


class DoneEvent(BaseModel):
    type: Literal["done"] = "done"
    session_id: str
    title: str
    position: str
    qa_count: int
    analysis_success_count: int
    warnings: list[str] = Field(default_factory=list)
    interview_review_md: str = ""
    links: dict[str, str] = Field(default_factory=dict)


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    session_id: str = ""
    message: str


class PausedEvent(BaseModel):
    type: Literal["paused"] = "paused"
    session_id: str
    completed_count: int = 0
    total_count: int = 0
    message: str = "分析已暂停"


class ResumedEvent(BaseModel):
    type: Literal["resumed"] = "resumed"
    session_id: str
    completed_count: int = 0
    total_count: int = 0
    message: str = "分析已继续"


PipelineEvent = (
    ProgressEvent
    | SessionReadyEvent
    | QADoneEvent
    | DoneEvent
    | ErrorEvent
    | PausedEvent
    | ResumedEvent
)


def event_to_sse(event: PipelineEvent | dict[str, Any]) -> str:
    if hasattr(event, "model_dump"):
        payload = event.model_dump()
        etype = payload.get("type", "message")
    else:
        payload = event
        etype = payload.get("type", "message")
    import json

    data = json.dumps(payload, ensure_ascii=False)
    return f"event: {etype}\ndata: {data}\n\n"
