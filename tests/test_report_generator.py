"""FR6 报告生成单元测试（不依赖大模型）。"""

from src.models.schemas import AnalysisResult, QAItem, ResumeSummary
from src.services.report_generator import generate_review_markdown


def test_generate_review_contains_four_parts():
    qa = QAItem(id="Q1", topic="基础", question="你为什么想换工作？", answer="寻求成长。")
    analysis = AnalysisResult(
        qa_id="Q1",
        assessment_point="动机与稳定性",
        original_answer="寻求成长。",
        issues=["【类型】缺量化：引用「寻求成长」+ 缺少具体目标"],
        improved_answer="希望在更大业务体量中沉淀方法论。",
        position="后端工程师",
        success=True,
    )
    md = generate_review_markdown(
        qa_items=[qa],
        analyses=[analysis],
        resume_summary=ResumeSummary(),
        position="后端工程师",
    )
    assert "# 面试复盘报告" in md
    assert "### 1. 问题考察点" in md
    assert "### 4. 合理回答（改进示例）" in md
    assert "后端工程师" in md
