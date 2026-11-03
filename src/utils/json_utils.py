"""从模型输出中稳健提取 JSON 对象。"""

from __future__ import annotations

import json
import re
from typing import Any, Optional


def extract_json_object(text: str) -> Optional[dict[str, Any]]:
    """尝试从可能含 markdown 代码块或前后噪音的文本中解析 JSON 对象。"""
    if not text or not text.strip():
        return None

    cleaned = text.strip()

    # 去掉 ```json ... ``` 包裹
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    # 直接解析
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 截取首尾花括号
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            return None
    return None
