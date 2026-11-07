"""
提示词模板包。

各 FR 对应的提示词以独立 .txt 文件存放，便于人工审查与迭代。
业务代码通过 load_prompt / render_prompt 读取并填充变量。
"""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent

PROMPT_FILES = {
    "resume_summary": "resume_summary.txt",
    "dialogue_organize": "dialogue_organize.txt",  # 兼容旧一站式整理
    "dialogue_transcribe": "dialogue_transcribe.txt",
    "dialogue_clarify_questions": "dialogue_clarify_questions.txt",
    "dialogue_build_qa": "dialogue_build_qa.txt",
    # 会话级语义压缩（分析前一次，多题复用）
    "context_compress_resume": "context_compress_resume.txt",
    "context_compress_jd": "context_compress_jd.txt",
    "context_compress_notes": "context_compress_notes.txt",
    # 逐题分析两步串行（考察点 → 不妥之处+改进回答）
    "qa_assessment": "qa_assessment.txt",
    "qa_assessment_with_jd": "qa_assessment_with_jd.txt",
    "qa_critique_improve": "qa_critique_improve.txt",
    "qa_critique_improve_with_jd": "qa_critique_improve_with_jd.txt",
    "qa_revise": "qa_revise.txt",
}


def load_prompt(name: str) -> str:
    if name not in PROMPT_FILES:
        raise KeyError(f"未知提示词: {name}，可选: {list(PROMPT_FILES)}")
    path = PROMPTS_DIR / PROMPT_FILES[name]
    if not path.exists():
        raise FileNotFoundError(f"提示词文件不存在: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **kwargs: str) -> str:
    text = load_prompt(name)
    for key, value in kwargs.items():
        text = text.replace("{{" + key + "}}", value if value is not None else "")
    return text
