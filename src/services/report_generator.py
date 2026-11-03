"""
FR6 — 生成最终 interview_review.md 报告。

需求5：同一题可有多版本，按 V1→Vn 顺序叠加。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

from src.models.schemas import AnalysisResult, QAItem, ResumeSummary


def _topic_stats(qa_items: list[QAItem]) -> list[tuple[str, int]]:
    c = Counter((q.topic or "未标注").strip() or "未标注" for q in qa_items)
    return c.most_common()


def _group_analyses_by_qa(
    analyses: list[AnalysisResult],
) -> dict[str, list[AnalysisResult]]:
    grouped: dict[str, list[AnalysisResult]] = defaultdict(list)
    for a in analyses:
        grouped[a.qa_id].append(a)
    for qa_id, lst in grouped.items():
        lst.sort(key=lambda x: int(x.version or 1))
    return grouped


def _append_one_version_body(
    lines: list[str],
    *,
    a: AnalysisResult,
    qa: QAItem,
    show_version_header: bool,
) -> None:
    if show_version_header:
        lines.append(f"### 版本 {a.version_label}")
        lines.append("")
        if (a.user_feedback or "").strip():
            lines.append(f"（用户要求：{a.user_feedback.strip()}）")
            lines.append("")

    h = "####" if show_version_header else "###"
    if not a.success:
        lines.append(f"{h} 1. 问题考察点")
        lines.append("")
        lines.append("_分析失败或未完成，以下为原文降级展示。_")
        lines.append("")
        lines.append(f"{h} 2. 原文回答")
        lines.append("")
        lines.append(qa.answer or "_（空）_")
        lines.append("")
        lines.append(f"{h} 3. 原回答不妥之处")
        lines.append("")
        lines.append(f"_无法生成（{a.error or '无分析结果'}）_")
        lines.append("")
        lines.append(f"{h} 4. 合理回答（改进示例）")
        lines.append("")
        lines.append("_无法生成_")
        lines.append("")
        return

    if a.assessment_point or a.issues or a.improved_answer:
        lines.append(f"{h} 1. 问题考察点")
        lines.append("")
        lines.append(a.assessment_point or "_（空）_")
        lines.append("")
        lines.append(f"{h} 2. 原文回答")
        lines.append("")
        lines.append(a.original_answer or qa.answer or "_（空）_")
        lines.append("")
        lines.append(f"{h} 3. 原回答不妥之处")
        lines.append("")
        if a.issues:
            for issue in a.issues:
                lines.append(f"- {issue}")
        else:
            lines.append("_（未识别到明确不妥之处）_")
        lines.append("")
        lines.append(f"{h} 4. 合理回答（改进示例）")
        lines.append("")
        lines.append(a.improved_answer or "_（空）_")
        lines.append("")
    else:
        lines.append(a.raw_markdown)
        lines.append("")


def generate_review_markdown(
    *,
    qa_items: list[QAItem],
    analyses: list[AnalysisResult],
    resume_summary: Optional[ResumeSummary],
    position: str = "待确认",
    extra_warnings: Optional[list[str]] = None,
) -> str:
    """组装最终复盘报告 markdown（支持每题多版本叠加）。"""
    by_qa = _group_analyses_by_qa(analyses)
    topics = _topic_stats(qa_items)
    topic_line = "、".join(f"{t}({n})" for t, n in topics[:8]) or "暂无"

    warnings = list(extra_warnings or [])
    if resume_summary is None:
        warnings.append("无简历参照，改进回答受限")
    elif resume_summary.is_fallback_text:
        warnings.append("简历摘要为降级纯文本，结构化信息可能不完整")

    # 失败题：按「该题最新版」是否失败计
    failed = 0
    for qa in qa_items:
        vers = by_qa.get(qa.id) or []
        if not vers:
            failed += 1
            continue
        if not vers[-1].success:
            failed += 1

    lines: list[str] = []
    lines.append("# 面试复盘报告")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("## 汇总")
    lines.append("")
    lines.append(f"- **总题数**：{len(qa_items)}")
    lines.append(f"- **推断岗位**：{position or '待确认'}")
    lines.append(f"- **高频问题类型**：{topic_line}")
    if failed:
        lines.append(f"- **分析失败题数**：{failed}（以各题最新版计；已降级展示原文）")
    if warnings:
        lines.append("- **提示**：")
        for w in warnings:
            lines.append(f"  - {w}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for qa in qa_items:
        vers = by_qa.get(qa.id) or []
        parent = f"（追问自 {qa.parent_id}）" if qa.parent_id else ""
        lines.append(f"## {qa.id}｜话题：{qa.topic}{parent}")
        lines.append("")
        lines.append(f"**问：** {qa.question}")
        lines.append("")

        if not vers:
            _append_one_version_body(
                lines, a=AnalysisResult(qa_id=qa.id, success=False, error="无分析结果"), qa=qa, show_version_header=False
            )
            continue

        multi = len(vers) > 1
        for a in vers:
            _append_one_version_body(lines, a=a, qa=qa, show_version_header=multi)

        if qa.flags.warnings:
            lines.append(f"> 切分标记：{'；'.join(qa.flags.warnings)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
