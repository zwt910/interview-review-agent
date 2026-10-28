"""
FR4 — 将 interview_qa.md 切分为 QAItem 列表，并做一致性校验。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from config.llm_config import get_settings
from src.models.schemas import QAFlags, QAItem

logger = logging.getLogger(__name__)

# ## Q1｜话题：xxx  或  ## Q1|话题：xxx  或  ## Q1 话题：xxx
_HEADER_RE = re.compile(
    r"^##\s*Q(?P<num>\d+)\s*[｜|]\s*话题[：:]\s*(?P<topic>.+?)\s*$",
    re.MULTILINE,
)
_PARENT_RE = re.compile(r"^-\s*追问自[：:]\s*Q(?P<num>\d+)\s*$", re.MULTILINE)
_Q_RE = re.compile(r"\*\*问[：:]\*\*\s*(?P<body>[\s\S]*?)(?=\*\*答[：:]\*\*|\n##\s*Q|\Z)")
_A_RE = re.compile(r"\*\*答[：:]\*\*\s*(?P<body>[\s\S]*?)(?=\n##\s*Q|\Z)")


def _split_blocks(markdown: str) -> list[str]:
    """按 ## Qn 标题切块。"""
    parts = re.split(r"(?=^##\s*Q\d+)", markdown.strip(), flags=re.MULTILINE)
    return [p.strip() for p in parts if p.strip() and re.match(r"^##\s*Q\d+", p.strip())]


def split_qa_markdown(markdown: str) -> list[QAItem]:
    """切分并校验。"""
    settings = get_settings()
    blocks = _split_blocks(markdown)
    header_count = len(_HEADER_RE.findall(markdown))

    items: list[QAItem] = []
    seen_ids: set[str] = set()

    for block in blocks:
        hm = _HEADER_RE.search(block)
        if not hm:
            logger.warning("跳过无法解析表头的块: %s", block[:80])
            continue

        qid = f"Q{hm.group('num')}"
        topic = hm.group("topic").strip()

        parent_id: Optional[str] = None
        pm = _PARENT_RE.search(block)
        if pm:
            parent_id = f"Q{pm.group('num')}"

        qm = _Q_RE.search(block)
        am = _A_RE.search(block)
        question = (qm.group("body").strip() if qm else "").strip()
        answer = (am.group("body").strip() if am else "").strip()

        flags = QAFlags()
        if len(answer) < settings.empty_answer_threshold:
            flags.empty_answer = True
            flags.warnings.append("空答或回答过短")
        if len(answer) > settings.long_answer_threshold:
            flags.long_answer = True
            flags.warnings.append("回答超长")

        if qid in seen_ids:
            flags.warnings.append(f"重复 id: {qid}")
        seen_ids.add(qid)

        items.append(
            QAItem(
                id=qid,
                parent_id=parent_id,
                topic=topic,
                question=question,
                answer=answer,
                flags=flags,
            )
        )

    # 校验：条目数与标题数一致
    if header_count and len(items) != header_count:
        logger.warning(
            "切分条目数(%s)与 markdown 标题数(%s)不一致",
            len(items),
            header_count,
        )
        for it in items:
            it.flags.warnings.append(
                f"切分条目数({len(items)})与标题数({header_count})不一致"
            )

    # 校验追问关系
    id_set = {it.id for it in items}
    for it in items:
        if it.parent_id and it.parent_id not in id_set:
            it.flags.warnings.append(f"parent_id {it.parent_id} 不存在")
            logger.warning("%s 的 parent_id=%s 无效", it.id, it.parent_id)

    if not items:
        raise ValueError("切分结果为空：markdown 中未找到有效问答对")

    return items
