"""简历注释（可选）格式化，供提示词注入。未填写时返回空串，行为与旧流程一致。"""

from __future__ import annotations


def normalize_resume_notes(notes: str | None) -> str:
    return (notes or "").strip()


def format_resume_notes_block(notes: str | None) -> str:
    """生成可直接嵌入提示词的【简历注释】段落；无内容时返回空字符串。"""
    text = normalize_resume_notes(notes)
    if not text:
        return ""
    return (
        "\n【简历注释】\n"
        "以下为用户对简历内容的补充说明，须优先参考并与简历正文交叉印证，"
        "用于补全背景、澄清项目细节与量化口径；不要忽略或与之矛盾。\n"
        f"{text}\n"
    )
