"""会话（Session）数据结构 — 类似 DeepSeek 侧边栏历史会话。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from src.models.schemas import AnalysisResult, QAItem, ResumeSummary


class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # 需求3：文稿解析向导中间态
    AWAITING_TRANSCRIPT = "awaiting_transcript"
    AWAITING_QUESTIONS = "awaiting_questions"
    AWAITING_QA = "awaiting_qa"
    ANALYZING = "analyzing"
    PAUSED = "paused"


class SessionMeta(BaseModel):
    """会话列表项 / 元数据（轻量，用于侧边栏）。旧字段保留，新字段有默认值以兼容旧数据。"""

    session_id: str
    title: str
    status: SessionStatus = SessionStatus.PENDING
    # 向导步骤：uploaded | transcript | questions | qa | analyzing | done
    workflow_step: str = "uploaded"
    position: str = "待确认"
    qa_count: int = 0
    analysis_success_count: int = 0
    has_resume: bool = False
    has_job_description: bool = False
    has_resume_notes: bool = False
    resume_filename: Optional[str] = None
    dialogue_filename: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    error: Optional[str] = None
    warnings: list[str] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat(timespec="seconds")


class SessionDetail(BaseModel):
    """会话详情：含全部已生成内容，读取时不调用模型。"""

    meta: SessionMeta
    resume_summary: Optional[ResumeSummary] = None
    qa_items: list[QAItem] = Field(default_factory=list)
    analyses: list[AnalysisResult] = Field(default_factory=list)
    interview_qa_md: str = ""
    interview_review_md: str = ""
    dialogue_raw: str = ""
    resume_raw: str = ""
    job_description: str = ""
    resume_notes: str = ""
    # 需求3 中间产物
    cleaned_transcript: str = ""
    clarified_questions: str = ""
    artifact_names: list[str] = Field(default_factory=list)


class SessionCreateResponse(BaseModel):
    """创建并跑完流水线后的响应。"""

    session_id: str
    title: str
    status: SessionStatus
    position: str
    qa_count: int
    analysis_success_count: int
    warnings: list[str] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)
    privacy_notice: str = "本服务不脱敏，请自行注意隐私。"


def default_session_title(
    *,
    position: str = "待确认",
    dialogue_filename: Optional[str] = None,
    created_at: Optional[str] = None,
) -> str:
    """自动生成侧边栏标题。"""
    ts = (created_at or datetime.now().isoformat(timespec="seconds"))[:16].replace("T", " ")
    pos = (position or "待确认").strip()
    if dialogue_filename:
        stem = dialogue_filename.rsplit(".", 1)[0]
        if stem:
            return f"{stem} · {pos}"
    return f"面试复盘 · {pos} · {ts}"
