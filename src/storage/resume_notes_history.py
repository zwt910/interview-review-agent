"""
简历注释历史（跨会话复用）。

落盘：data/resume_notes_history.json
保存前与已有正文做匹配：完全一致则跳过不写；最多保留近 10 条不同注释。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from config.llm_config import PROJECT_ROOT, get_settings
from src.utils.resume_notes import normalize_resume_notes

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 10


class ResumeNotesHistoryStore:
    def __init__(self, path: Optional[Path] = None, *, max_items: int = DEFAULT_MAX_ITEMS):
        settings = get_settings()
        data_root = Path(settings.sessions_dir).parent
        self.path = Path(path or (data_root / "resume_notes_history.json"))
        self.max_items = max(1, int(max_items))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("读取简历注释历史失败，将重建: %s", e)
            return []
        if not isinstance(raw, list):
            return []
        items: list[dict[str, Any]] = []
        for x in raw:
            if not isinstance(x, dict):
                continue
            text = normalize_resume_notes(str(x.get("text") or ""))
            if not text:
                continue
            items.append(
                {
                    "id": str(x.get("id") or uuid.uuid4().hex[:10]),
                    "text": text,
                    "saved_at": str(x.get("saved_at") or ""),
                    "preview": str(x.get("preview") or self._preview(text)),
                }
            )
        # 兼容旧数据：去重并裁剪到上限（列表前部为较新）
        cleaned = self._dedupe_keep_order(items)[: self.max_items]
        if cleaned != items:
            try:
                self._write(cleaned)
            except OSError as e:
                logger.warning("裁剪简历注释历史落盘失败: %s", e)
        return cleaned

    def _write(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _preview(text: str, limit: int = 72) -> str:
        one = " ".join(text.split())
        if len(one) <= limit:
            return one
        return one[: limit - 1] + "…"

    @staticmethod
    def _dedupe_keep_order(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for x in items:
            text = str(x.get("text") or "")
            if not text or text in seen:
                continue
            seen.add(text)
            out.append(x)
        return out

    def list_items(self, *, limit: int = 10) -> list[dict[str, Any]]:
        items = self._read()
        lim = max(1, min(int(limit), self.max_items))
        return items[:lim]

    def add(self, text: str) -> Optional[dict[str, Any]]:
        """写入一条历史；空文本忽略。

        若与已有任一条正文完全一致（normalize 后），则不保存、不改动列表。
        新内容插入队首，超出 max_items 时丢掉最旧的。
        """
        content = normalize_resume_notes(text)
        if not content:
            return None
        items = self._read()
        if any(x.get("text") == content for x in items):
            logger.info(
                "简历注释历史跳过重复保存 chars=%s total=%s",
                len(content),
                len(items),
            )
            return None

        now = datetime.now().isoformat(timespec="seconds")
        entry = {
            "id": uuid.uuid4().hex[:10],
            "text": content,
            "saved_at": now,
            "preview": self._preview(content),
        }
        items.insert(0, entry)
        items = items[: self.max_items]
        self._write(items)
        logger.info(
            "简历注释历史已保存 id=%s chars=%s total=%s",
            entry["id"],
            len(content),
            len(items),
        )
        return entry

    def delete(self, item_id: str) -> bool:
        iid = (item_id or "").strip()
        if not iid:
            return False
        items = self._read()
        new_items = [x for x in items if x.get("id") != iid]
        if len(new_items) == len(items):
            return False
        self._write(new_items)
        logger.info("简历注释历史已删除 id=%s remaining=%s", iid, len(new_items))
        return True


@lru_cache
def get_resume_notes_history_store() -> ResumeNotesHistoryStore:
    return ResumeNotesHistoryStore()
