"""请求上下文：把 access 的 req_id 与业务 session_id 串进 app.log。"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

_req_id: ContextVar[str] = ContextVar("req_id", default="")
_session_id: ContextVar[str] = ContextVar("session_id", default="")


def set_request_id(request_id: str) -> None:
    _req_id.set((request_id or "").strip())


def get_request_id() -> str:
    return _req_id.get() or ""


def set_session_id(session_id: str) -> None:
    _session_id.set((session_id or "").strip())


def get_session_id() -> str:
    return _session_id.get() or ""


def clear_context() -> None:
    _req_id.set("")
    _session_id.set("")


def with_ctx(msg: str, *, session_id: Optional[str] = None) -> str:
    """给日志消息加上 [req=… session=…] 前缀。"""
    rid = get_request_id()
    sid = (session_id or "").strip() or get_session_id()
    parts: list[str] = []
    if rid:
        parts.append(f"req={rid}")
    if sid:
        parts.append(f"session={sid}")
    if not parts:
        return msg
    return f"[{' '.join(parts)}] {msg}"
