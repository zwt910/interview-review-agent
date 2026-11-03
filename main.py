"""
本地 CLI 入口（无需前端即可跑通流水线）。

示例：
    python main.py --dialogue path/to/dialogue.docx --resume path/to/resume.pdf
    python main.py --list
    python main.py --show <session_id>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证以项目根为 sys.path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def cmd_list(limit: int) -> int:
    from src.storage.session_store import get_session_store

    store = get_session_store()
    sessions = store.list_sessions(limit=limit)
    if not sessions:
        print("暂无历史会话")
        return 0
    for m in sessions:
        print(
            f"{m.session_id}\t{m.status.value}\tQ{m.qa_count}\t{m.title}\t{m.updated_at}"
        )
    return 0


def cmd_show(session_id: str) -> int:
    from src.storage.session_store import get_session_store

    store = get_session_store()
    try:
        detail = store.get_detail(session_id)
    except FileNotFoundError:
        print(f"会话不存在: {session_id}", file=sys.stderr)
        return 1
    m = detail.meta
    print(f"session_id={m.session_id}")
    print(f"title={m.title}")
    print(f"status={m.status.value}")
    print(f"position={m.position}")
    print(f"qa_count={m.qa_count}")
    print(f"artifacts={detail.artifact_names}")
    print("--- interview_review.md (前 800 字) ---")
    print((detail.interview_review_md or "")[:800])
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    dialogue_path = Path(args.dialogue)
    if not dialogue_path.exists():
        print(f"对话文件不存在: {dialogue_path}", file=sys.stderr)
        return 1

    resume_bytes = None
    resume_name = None
    if args.resume:
        resume_path = Path(args.resume)
        if not resume_path.exists():
            print(f"简历文件不存在: {resume_path}", file=sys.stderr)
            return 1
        resume_bytes = resume_path.read_bytes()
        resume_name = resume_path.name

    if not args.position or not str(args.position).strip():
        print("面试岗位为必填，请使用 --position", file=sys.stderr)
        return 1

    jd_text = ""
    if args.jd:
        jd_path = Path(args.jd)
        if not jd_path.exists():
            print(f"JD 文件不存在: {jd_path}", file=sys.stderr)
            return 1
        if jd_path.suffix.lower() == ".docx":
            from src.parsers.docx_parser import parse_docx

            jd_text = parse_docx(jd_path.read_bytes(), source_type="jd_docx", filename=jd_path.name).text
        else:
            jd_text = jd_path.read_text(encoding="utf-8")
    if args.jd_text:
        jd_text = ((jd_text + "\n\n") if jd_text else "") + args.jd_text

    from src.pipeline.review_pipeline import InterviewReviewPipeline

    pipeline = InterviewReviewPipeline()
    result, meta = pipeline.run(
        dialogue_bytes=dialogue_path.read_bytes(),
        dialogue_filename=dialogue_path.name,
        resume_bytes=resume_bytes,
        resume_filename=resume_name,
        position=args.position.strip(),
        job_description=jd_text,
        job_id=args.job_id,
        persist=not args.no_persist,
        title=args.title,
        save_session=not args.no_session,
    )

    print(f"session_id={result.job_id}")
    if meta:
        print(f"title={meta.title}")
        print(f"status={meta.status.value}")
    print(f"position={result.position}")
    print(f"qa_count={len(result.qa_items)}")
    print(f"warnings={result.warnings}")
    if meta:
        print(f"session_dir=data/sessions/{meta.session_id}")
        print(f"view: python main.py --show {meta.session_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="面试复盘助手 CLI")
    parser.add_argument("--dialogue", default=None, help="面试对话 .docx 路径")
    parser.add_argument("--resume", default=None, help="简历 .pdf/.docx 路径（可选）")
    parser.add_argument("--position", default=None, help="面试岗位（必填）")
    parser.add_argument("--jd", default=None, help="岗位 JD 文件 .docx/.txt（可选）")
    parser.add_argument("--jd-text", default=None, help="岗位 JD 纯文本（可选）")
    parser.add_argument("--title", default=None, help="会话标题")
    parser.add_argument("--no-persist", action="store_true", help="不写入 output/ 兼容目录")
    parser.add_argument("--no-session", action="store_true", help="不写入会话存储")
    parser.add_argument("--job-id", default=None, help="指定会话/任务 ID")
    parser.add_argument("--list", action="store_true", help="列出历史会话")
    parser.add_argument("--show", default=None, metavar="SESSION_ID", help="查看历史会话（不调模型）")
    parser.add_argument("--limit", type=int, default=30, help="列表条数")
    args = parser.parse_args()

    from config.llm_config import get_settings
    from src.utils.logging_setup import setup_logging

    settings = get_settings()
    setup_logging(
        level=settings.log_level,
        log_dir=settings.log_dir,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        force=True,
    )

    if args.list:
        return cmd_list(args.limit)
    if args.show:
        return cmd_show(args.show)
    if not args.dialogue:
        parser.error("请提供 --dialogue，或使用 --list / --show")
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
