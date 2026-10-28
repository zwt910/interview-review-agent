"""
领域数据结构（与需求文档第 5 节对齐）。

字段命名采用英文便于代码审查；序列化到提示词/报告时再转中文 JSON。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ResumeSummary(BaseModel):
    """FR2 简历结构化摘要。"""

    basic: dict[str, Any] = Field(default_factory=dict, description="基本信息")
    education: list[dict[str, Any]] = Field(default_factory=list, description="教育")
    experiences: list[dict[str, Any]] = Field(default_factory=list, description="工作经历")
    projects: list[dict[str, Any]] = Field(default_factory=list, description="项目")
    skills: list[str] = Field(default_factory=list, description="技能")
    quantified_results: list[str] = Field(default_factory=list, description="量化成果")
    highlights: list[str] = Field(default_factory=list, description="亮点")
    # True 表示 JSON 解析失败后降级为纯文本摘要
    is_fallback_text: bool = False
    raw_text_fallback: Optional[str] = None

    def to_prompt_json(self) -> dict[str, Any]:
        """转为提示词中的中文 key JSON。"""
        if self.is_fallback_text and self.raw_text_fallback:
            return {"摘要文本": self.raw_text_fallback}
        return {
            "基本信息": self.basic,
            "教育": self.education,
            "工作经历": self.experiences,
            "项目": self.projects,
            "技能": self.skills,
            "量化成果": self.quantified_results,
            "亮点": self.highlights,
        }

    @classmethod
    def from_llm_json(cls, data: dict[str, Any]) -> ResumeSummary:
        return cls(
            basic=data.get("基本信息") or data.get("basic") or {},
            education=data.get("教育") or data.get("education") or [],
            experiences=data.get("工作经历") or data.get("experiences") or [],
            projects=data.get("项目") or data.get("projects") or [],
            skills=data.get("技能") or data.get("skills") or [],
            quantified_results=data.get("量化成果") or data.get("quantified_results") or [],
            highlights=data.get("亮点") or data.get("highlights") or [],
        )

    @classmethod
    def from_fallback_text(cls, text: str) -> ResumeSummary:
        return cls(is_fallback_text=True, raw_text_fallback=text[:4000])


class QAFlags(BaseModel):
    """FR4 切分校验标记。"""

    empty_answer: bool = False
    long_answer: bool = False
    warnings: list[str] = Field(default_factory=list)


class QAItem(BaseModel):
    """FR4 切分后的问答对。"""

    id: str
    parent_id: Optional[str] = None
    topic: str = ""
    question: str
    answer: str
    flags: QAFlags = Field(default_factory=QAFlags)


class AnalysisResult(BaseModel):
    """FR5 单题分析结果。"""

    qa_id: str
    assessment_point: str = ""
    original_answer: str = ""
    issues: list[str] = Field(default_factory=list)
    improved_answer: str = ""
    resume_refs: list[str] = Field(default_factory=list)
    position: str = "待确认"
    # 解析失败时保留模型原文，便于降级展示
    raw_markdown: str = ""
    success: bool = True
    error: Optional[str] = None


class ParseResult(BaseModel):
    """FR1 文本提取结果。"""

    text: str
    source_type: str  # resume_pdf / resume_docx / dialogue_docx
    page_count: Optional[int] = None
    warnings: list[str] = Field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None


class PipelineResult(BaseModel):
    """端到端流水线输出。"""

    job_id: str
    position: str = "待确认"
    resume_summary: Optional[ResumeSummary] = None
    qa_items: list[QAItem] = Field(default_factory=list)
    analyses: list[AnalysisResult] = Field(default_factory=list)
    interview_qa_md: str = ""
    interview_review_md: str = ""
    warnings: list[str] = Field(default_factory=list)
    # 中间产物路径（若开启持久化）
    artifact_paths: dict[str, str] = Field(default_factory=dict)
