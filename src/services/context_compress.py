"""
会话级分析上下文语义压缩。

在逐题分析前调用一次（简历 / JD / 注释），结果落盘后多题与修订复用。
失败时降级为程序化紧凑 JSON / 原文；硬截断仅作保险丝。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from prompts import render_prompt
from src.llm.client import LLMClient, get_llm_client_for
from src.models.schemas import ResumeSummary
from src.services.resume_summary import (
    resume_summary_to_compact_json_str,
    resume_summary_to_json_str,
)
from src.storage.session_store import SessionStore
from src.utils.json_utils import extract_json_object
from src.utils.request_context import with_ctx
from src.utils.resume_notes import normalize_resume_notes

logger = logging.getLogger(__name__)

ARTIFACT_RESUME = "analysis_ctx_resume.json"
ARTIFACT_JD = "analysis_ctx_jd.json"
ARTIFACT_NOTES = "analysis_ctx_notes.json"

# 低于阈值则不做 LLM 压缩（本身已短）
JD_LLM_MIN_CHARS = 600
NOTES_LLM_MIN_CHARS = 400

# 语义压缩后的保险丝上限（字符）
RESUME_FUSE_MAX = 3200
JD_FUSE_MAX = 2200
NOTES_FUSE_MAX = 1400


@dataclass
class AnalysisContextPack:
    """注入逐题分析 / 修订的压缩上下文。"""

    resume_json: str
    job_description: str
    resume_notes: str
    resume_source: str = "none"  # semantic | programmatic | cached | none
    jd_source: str = "none"
    notes_source: str = "none"


def _fuse(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if max_chars <= 0 or len(s) <= max_chars:
        return s
    return s[: max_chars - 1].rstrip() + "…"


def _dumps_compact(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _read_artifact(store: SessionStore, session_id: str, name: str) -> str:
    path = store.artifacts_dir(session_id) / name
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _llm_compress_to_json(
    *,
    prompt_name: str,
    client: LLMClient,
    max_tokens: int = 2048,
    **vars: str,
) -> Optional[str]:
    prompt = render_prompt(prompt_name, **vars)
    raw = client.chat(
        prompt,
        system="你只输出合法 JSON 对象，不要 markdown，不要解释。",
        temperature=0.1,
        max_tokens=max_tokens,
    )
    obj = extract_json_object(raw)
    if obj is None:
        return None
    return _dumps_compact(obj)


def _compress_resume(
    summary: Optional[ResumeSummary],
    *,
    position: str,
    client: LLMClient,
) -> tuple[str, str]:
    if summary is None:
        return '{"提示":"无简历参照，改进回答受限"}', "none"

    full = resume_summary_to_json_str(summary)
    programmatic = resume_summary_to_compact_json_str(summary, max_chars=RESUME_FUSE_MAX)

    # 已很短则直接用程序压缩结果，省一次调用
    if len(full) <= 1200:
        return _fuse(programmatic, RESUME_FUSE_MAX), "programmatic"

    try:
        semantic = _llm_compress_to_json(
            prompt_name="context_compress_resume",
            client=client,
            max_tokens=2048,
            position=position or "待确认",
            resume_summary_json=full,
        )
        if semantic:
            return _fuse(semantic, RESUME_FUSE_MAX), "semantic"
        logger.warning(with_ctx("简历语义压缩解析失败，降级为程序压缩"))
    except Exception as e:
        logger.warning(with_ctx("简历语义压缩失败，降级为程序压缩: %s"), e)
    return _fuse(programmatic, RESUME_FUSE_MAX), "programmatic"


def _compress_jd(
    jd: str,
    *,
    position: str,
    client: LLMClient,
) -> tuple[str, str]:
    text = (jd or "").strip()
    if not text:
        return "", "none"
    if len(text) < JD_LLM_MIN_CHARS:
        # 短 JD：包一层 JSON，保持形式统一
        return _fuse(_dumps_compact({"岗位JD要点": text}), JD_FUSE_MAX), "passthrough"

    try:
        semantic = _llm_compress_to_json(
            prompt_name="context_compress_jd",
            client=client,
            max_tokens=1536,
            position=position or "待确认",
            job_description=text,
        )
        if semantic:
            return _fuse(semantic, JD_FUSE_MAX), "semantic"
        logger.warning(with_ctx("JD 语义压缩解析失败，降级为截断原文 JSON"))
    except Exception as e:
        logger.warning(with_ctx("JD 语义压缩失败，降级: %s"), e)
    return _fuse(_dumps_compact({"岗位JD要点": _fuse(text, JD_FUSE_MAX - 40)}), JD_FUSE_MAX), "programmatic"


def _compress_notes(
    notes: str,
    *,
    position: str,
    client: LLMClient,
) -> tuple[str, str]:
    text = normalize_resume_notes(notes)
    if not text:
        return "", "none"
    if len(text) < NOTES_LLM_MIN_CHARS:
        return _fuse(_dumps_compact({"补充说明": text}), NOTES_FUSE_MAX), "passthrough"

    try:
        semantic = _llm_compress_to_json(
            prompt_name="context_compress_notes",
            client=client,
            max_tokens=1024,
            position=position or "待确认",
            resume_notes=text,
        )
        if semantic:
            return _fuse(semantic, NOTES_FUSE_MAX), "semantic"
        logger.warning(with_ctx("注释语义压缩解析失败，降级"))
    except Exception as e:
        logger.warning(with_ctx("注释语义压缩失败，降级: %s"), e)
    return _fuse(_dumps_compact({"补充说明": _fuse(text, NOTES_FUSE_MAX - 40)}), NOTES_FUSE_MAX), "programmatic"


def load_or_build_analysis_context(
    store: SessionStore,
    session_id: str,
    *,
    resume_summary: Optional[ResumeSummary],
    job_description: str = "",
    resume_notes: str = "",
    position: str = "待确认",
    force: bool = False,
    client: Optional[LLMClient] = None,
) -> AnalysisContextPack:
    """读取已缓存的压缩上下文；缺失则语义压缩并落盘。

    压缩模型默认用 dialogue 阶段（通常更便宜的 flash）。
    """
    pos = position or "待确认"
    cached_resume = "" if force else _read_artifact(store, session_id, ARTIFACT_RESUME)
    cached_jd = "" if force else _read_artifact(store, session_id, ARTIFACT_JD)
    cached_notes = "" if force else _read_artifact(store, session_id, ARTIFACT_NOTES)

    need_resume = resume_summary is not None and not cached_resume
    need_jd = bool((job_description or "").strip()) and not cached_jd
    need_notes = bool(normalize_resume_notes(resume_notes)) and not cached_notes

    llm = client
    if need_resume or need_jd or need_notes:
        llm = llm or get_llm_client_for("dialogue")
        logger.info(
            with_ctx(
                "分析上下文压缩开始 need_resume=%s need_jd=%s need_notes=%s",
                session_id=session_id,
            ),
            need_resume,
            need_jd,
            need_notes,
        )

    if cached_resume:
        resume_json, resume_src = cached_resume, "cached"
    elif resume_summary is None:
        resume_json, resume_src = '{"提示":"无简历参照，改进回答受限"}', "none"
        store.save_text_artifact(session_id, ARTIFACT_RESUME, resume_json)
    else:
        assert llm is not None
        resume_json, resume_src = _compress_resume(resume_summary, position=pos, client=llm)
        store.save_text_artifact(session_id, ARTIFACT_RESUME, resume_json)

    if cached_jd:
        jd_json, jd_src = cached_jd, "cached"
    elif not (job_description or "").strip():
        jd_json, jd_src = "", "none"
    else:
        assert llm is not None or not need_jd
        jd_json, jd_src = _compress_jd(
            job_description, position=pos, client=llm or get_llm_client_for("dialogue")
        )
        store.save_text_artifact(session_id, ARTIFACT_JD, jd_json)

    if cached_notes:
        notes_json, notes_src = cached_notes, "cached"
    elif not normalize_resume_notes(resume_notes):
        notes_json, notes_src = "", "none"
    else:
        notes_json, notes_src = _compress_notes(
            resume_notes, position=pos, client=llm or get_llm_client_for("dialogue")
        )
        store.save_text_artifact(session_id, ARTIFACT_NOTES, notes_json)

    logger.info(
        with_ctx(
            "分析上下文就绪 resume=%s/%s jd=%s/%s notes=%s/%s",
            session_id=session_id,
        ),
        resume_src,
        len(resume_json),
        jd_src,
        len(jd_json),
        notes_src,
        len(notes_json),
    )
    return AnalysisContextPack(
        resume_json=resume_json,
        job_description=jd_json,
        resume_notes=notes_json,
        resume_source=resume_src,
        jd_source=jd_src,
        notes_source=notes_src,
    )


def format_compressed_notes_block(notes_json: str) -> str:
    """将压缩后的注释 JSON 嵌入提示词。"""
    text = (notes_json or "").strip()
    if not text:
        return ""
    return (
        "\n【简历注释（语义压缩）】\n"
        "以下为用户注释的压缩要点，须优先参考并与简历交叉印证。\n"
        f"{text}\n"
    )
