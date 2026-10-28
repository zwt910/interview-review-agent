"""
会话持久化（本地文件系统）。

旧会话目录不删除；新增字段以缺省兼容读取。
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from config.llm_config import PROJECT_ROOT, get_settings
from src.models.schemas import AnalysisResult, PipelineResult, QAItem, ResumeSummary
from src.models.session import (
    SessionDetail,
    SessionMeta,
    SessionStatus,
    default_session_title,
)

logger = logging.getLogger(__name__)

ARTIFACT_FILES = {
    "dialogue_raw.txt",
    "resume_raw.txt",
    "job_description.txt",
    "resume_notes.txt",
    "cleaned_transcript.txt",
    "clarified_questions.md",
    "resume_summary.json",
    "interview_qa.md",
    "qa_items.json",
    "analyses.json",
    "interview_review.md",
}


class SessionStore:
    """会话仓储。"""

    def __init__(self, root: Optional[Path] = None):
        settings = get_settings()
        self.root = Path(root or getattr(settings, "sessions_dir", PROJECT_ROOT / "data" / "sessions"))
        self.root.mkdir(parents=True, exist_ok=True)

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def meta_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "meta.json"

    def artifacts_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "artifacts"

    def uploads_dir(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "uploads"

    def create(
        self,
        *,
        title: Optional[str] = None,
        dialogue_filename: Optional[str] = None,
        resume_filename: Optional[str] = None,
        position: str = "待确认",
        has_job_description: bool = False,
        has_resume_notes: bool = False,
        session_id: Optional[str] = None,
    ) -> SessionMeta:
        sid = session_id or uuid.uuid4().hex[:12]
        if self.exists(sid):
            raise ValueError(f"会话已存在: {sid}")

        d = self.session_dir(sid)
        d.mkdir(parents=True, exist_ok=False)
        self.artifacts_dir(sid).mkdir(parents=True, exist_ok=True)
        self.uploads_dir(sid).mkdir(parents=True, exist_ok=True)

        meta = SessionMeta(
            session_id=sid,
            title=title
            or default_session_title(position=position, dialogue_filename=dialogue_filename),
            status=SessionStatus.PENDING,
            position=position or "待确认",
            has_resume=bool(resume_filename),
            has_job_description=has_job_description,
            has_resume_notes=has_resume_notes,
            resume_filename=resume_filename,
            dialogue_filename=dialogue_filename,
        )
        self._write_meta(meta)
        return meta

    def save_upload(
        self,
        session_id: str,
        *,
        filename: str,
        data: bytes,
        kind: str,
    ) -> Path:
        uploads = self.uploads_dir(session_id)
        uploads.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix.lower() or ".bin"
        safe_name = f"{kind}{suffix}"
        path = uploads / safe_name
        path.write_bytes(data)
        orig = uploads / f"original_{kind}_{Path(filename).name}"
        try:
            orig.write_bytes(data)
        except OSError:
            pass
        return path

    def mark_running(self, session_id: str) -> SessionMeta:
        meta = self.get_meta(session_id)
        meta.status = SessionStatus.RUNNING
        meta.touch()
        self._write_meta(meta)
        return meta

    def mark_failed(self, session_id: str, error: str) -> SessionMeta:
        meta = self.get_meta(session_id)
        meta.status = SessionStatus.FAILED
        meta.error = error
        meta.touch()
        self._write_meta(meta)
        return meta

    def update_meta_fields(self, session_id: str, **fields: Any) -> SessionMeta:
        meta = self.get_meta(session_id)
        for k, v in fields.items():
            if hasattr(meta, k):
                setattr(meta, k, v)
        meta.touch()
        self._write_meta(meta)
        return meta

    def save_text_artifact(self, session_id: str, name: str, content: str) -> Path:
        if name not in ARTIFACT_FILES and not name.endswith((".txt", ".md", ".json")):
            raise ValueError(f"不允许的产物名: {name}")
        art = self.artifacts_dir(session_id)
        art.mkdir(parents=True, exist_ok=True)
        path = art / name
        path.write_text(content or "", encoding="utf-8")
        return path

    def save_json_artifact(self, session_id: str, name: str, obj: Any) -> Path:
        return self.save_text_artifact(
            session_id,
            name,
            json.dumps(obj, ensure_ascii=False, indent=2),
        )

    def append_analysis(self, session_id: str, analysis: AnalysisResult) -> list[AnalysisResult]:
        """增量追加单题分析结果（不覆盖其它题）。"""
        art = self.artifacts_dir(session_id)
        path = art / "analyses.json"
        items: list[dict] = []
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    items = raw
            except json.JSONDecodeError:
                items = []
        # 同 qa_id 则替换
        items = [x for x in items if x.get("qa_id") != analysis.qa_id]
        items.append(analysis.model_dump())
        self.save_json_artifact(session_id, "analyses.json", items)
        meta = self.get_meta(session_id)
        meta.analysis_success_count = sum(1 for x in items if x.get("success"))
        meta.touch()
        self._write_meta(meta)
        return [AnalysisResult.model_validate(x) for x in items]

    def save_pipeline_result(
        self,
        session_id: str,
        result: PipelineResult,
        *,
        title: Optional[str] = None,
        job_description: str = "",
    ) -> SessionMeta:
        art = self.artifacts_dir(session_id)
        art.mkdir(parents=True, exist_ok=True)

        def _w_text(name: str, content: str) -> None:
            (art / name).write_text(content or "", encoding="utf-8")

        def _w_json(name: str, obj: Any) -> None:
            (art / name).write_text(
                json.dumps(obj, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        _w_text("interview_qa.md", result.interview_qa_md)
        _w_text("interview_review.md", result.interview_review_md)
        if job_description:
            _w_text("job_description.txt", job_description)

        for key in ("dialogue_raw.txt", "resume_raw.txt", "job_description.txt"):
            src = result.artifact_paths.get(key)
            if src and Path(src).exists():
                _w_text(key, Path(src).read_text(encoding="utf-8"))

        if result.resume_summary is not None:
            _w_json("resume_summary.json", result.resume_summary.model_dump())
        _w_json("qa_items.json", [q.model_dump() for q in result.qa_items])
        _w_json("analyses.json", [a.model_dump() for a in result.analyses])

        meta = self.get_meta(session_id)
        meta.status = SessionStatus.COMPLETED
        meta.position = result.position or meta.position or "待确认"
        meta.qa_count = len(result.qa_items)
        meta.analysis_success_count = sum(1 for a in result.analyses if a.success)
        meta.has_job_description = bool(job_description) or meta.has_job_description
        meta.warnings = list(result.warnings or [])
        meta.error = None
        if title:
            meta.title = title
        meta.touch()
        self._write_meta(meta)
        return meta

    def rename(self, session_id: str, title: str) -> SessionMeta:
        meta = self.get_meta(session_id)
        meta.title = (title or "").strip() or meta.title
        meta.touch()
        self._write_meta(meta)
        return meta

    def exists(self, session_id: str) -> bool:
        return self.meta_path(session_id).is_file()

    def get_meta(self, session_id: str) -> SessionMeta:
        path = self.meta_path(session_id)
        if not path.exists():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionMeta.model_validate(data)

    def list_sessions(self, *, limit: int = 100, offset: int = 0) -> list[SessionMeta]:
        items: list[SessionMeta] = []
        if not self.root.exists():
            return []
        for d in self.root.iterdir():
            if not d.is_dir():
                continue
            meta_file = d / "meta.json"
            if not meta_file.exists():
                continue
            try:
                items.append(SessionMeta.model_validate(json.loads(meta_file.read_text(encoding="utf-8"))))
            except Exception as e:
                logger.warning("跳过损坏会话 %s: %s", d.name, e)
        items.sort(key=lambda m: m.updated_at, reverse=True)
        return items[offset : offset + limit]

    def get_detail(self, session_id: str) -> SessionDetail:
        meta = self.get_meta(session_id)
        art = self.artifacts_dir(session_id)

        def _read_text(name: str) -> str:
            p = art / name
            return p.read_text(encoding="utf-8") if p.exists() else ""

        def _read_json(name: str) -> Any:
            p = art / name
            if not p.exists():
                return None
            return json.loads(p.read_text(encoding="utf-8"))

        resume_summary = None
        rs = _read_json("resume_summary.json")
        if isinstance(rs, dict):
            resume_summary = ResumeSummary.model_validate(rs)

        qa_raw = _read_json("qa_items.json") or []
        qa_items = [QAItem.model_validate(x) for x in qa_raw] if isinstance(qa_raw, list) else []

        an_raw = _read_json("analyses.json") or []
        analyses = [AnalysisResult.model_validate(x) for x in an_raw] if isinstance(an_raw, list) else []

        artifact_names = sorted(p.name for p in art.glob("*") if p.is_file()) if art.exists() else []

        return SessionDetail(
            meta=meta,
            resume_summary=resume_summary,
            qa_items=qa_items,
            analyses=analyses,
            interview_qa_md=_read_text("interview_qa.md"),
            interview_review_md=_read_text("interview_review.md"),
            dialogue_raw=_read_text("dialogue_raw.txt"),
            resume_raw=_read_text("resume_raw.txt"),
            job_description=_read_text("job_description.txt"),
            resume_notes=_read_text("resume_notes.txt"),
            cleaned_transcript=_read_text("cleaned_transcript.txt"),
            clarified_questions=_read_text("clarified_questions.md"),
            artifact_names=artifact_names,
        )

    def get_artifact_path(self, session_id: str, name: str) -> Path:
        if name not in ARTIFACT_FILES:
            raise ValueError(f"不允许的产物名: {name}")
        if not self.exists(session_id):
            raise FileNotFoundError(f"会话不存在: {session_id}")
        path = self.artifacts_dir(session_id) / name
        if not path.exists():
            raise FileNotFoundError(f"产物不存在: {name}")
        return path

    def delete(self, session_id: str) -> None:
        d = self.session_dir(session_id)
        if not d.exists():
            raise FileNotFoundError(f"会话不存在: {session_id}")
        shutil.rmtree(d)

    def _write_meta(self, meta: SessionMeta) -> None:
        path = self.meta_path(meta.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(meta.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


@lru_cache
def get_session_store() -> SessionStore:
    return SessionStore()
