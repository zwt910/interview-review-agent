"""
FR6 — 生成最终 interview_review.md 报告。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Optional

from src.models.schemas import AnalysisResult, QAItem, ResumeSummary


def _topic_stats(qa_items: list[QAItem]) -> list[tuple[str, int]]:
    c = Counter((q.topic or "未标注").strip() or "未标注" for q in qa_items)
    return c.most_common()


def generate_review_markdown(
    *,
    qa_items: list[QAItem],
    analyses: list[AnalysisResult],
    resume_summary: Optional[ResumeSummary],
    position: str = "待确认",
    extra_warnings: Optional[list[str]] = None,
) -> str:
    """组装最终复盘报告 markdown。"""
    analysis_map = {a.qa_id: a for a in analyses}
    topics = _topic_stats(qa_items)
    topic_line = "、".join(f"{t}({n})" for t, n in topics[:8]) or "暂无"

    warnings = list(extra_warnings or [])
    if resume_summary is None:
        warnings.append("无简历参照，改进回答受限")
    elif resume_summary.is_fallback_text:
        warnings.append("简历摘要为降级纯文本，结构化信息可能不完整")

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
    failed = sum(1 for a in analyses if not a.success)
    if failed:
        lines.append(f"- **分析失败题数**：{failed}（已降级展示原文）")
    if warnings:
        lines.append("- **提示**：")
        for w in warnings:
            lines.append(f"  - {w}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for qa in qa_items:
        a = analysis_map.get(qa.id)
        parent = f"（追问自 {qa.parent_id}）" if qa.parent_id else ""
        lines.append(f"## {qa.id}｜话题：{qa.topic}{parent}")
        lines.append("")
        lines.append(f"**问：** {qa.question}")
        lines.append("")

        if a is None or not a.success:
            lines.append("### 1. 问题考察点")
            lines.append("")
            lines.append("_分析失败或未完成，以下为原文降级展示。_")
            lines.append("")
            lines.append("### 2. 原文回答")
            lines.append("")
            lines.append(qa.answer or "_（空）_")
            lines.append("")
            lines.append("### 3. 原回答不妥之处")
            lines.append("")
            lines.append(f"_无法生成（{a.error if a else '无分析结果'}）_")
            lines.append("")
            lines.append("### 4. 合理回答（改进示例）")
            lines.append("")
            lines.append("_无法生成_")
            lines.append("")
            continue

        # 优先用解析后的结构化字段；若某段为空则回退 raw
        if a.assessment_point or a.issues or a.improved_answer:
            lines.append("### 1. 问题考察点")
            lines.append("")
            lines.append(a.assessment_point or "_（空）_")
            lines.append("")
            lines.append("### 2. 原文回答")
            lines.append("")
            lines.append(a.original_answer or qa.answer or "_（空）_")
            lines.append("")
            lines.append("### 3. 原回答不妥之处")
            lines.append("")
            if a.issues:
                for issue in a.issues:
                    lines.append(f"- {issue}")
            else:
                lines.append("_（未识别到明确不妥之处）_")
            lines.append("")
            lines.append("### 4. 合理回答（改进示例）")
            lines.append("")
            lines.append(a.improved_answer or "_（空）_")
            lines.append("")
        else:
            lines.append(a.raw_markdown)
            lines.append("")

        if qa.flags.warnings:
            lines.append(f"> 切分标记：{'；'.join(qa.flags.warnings)}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
