"""逐题分析暂停 / 继续控制（进程内，按 session_id）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AnalysisController:
    """控制某次 analyze SSE：暂停后不再启动新题，继续后从剩余题接着跑。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._gate = asyncio.Event()
        self._gate.set()  # 未暂停时可继续领题
        self._paused = False
        self._closed = False
        # 与分析主循环共享：("paused"|"resumed"|"qa", payload)
        self.bridge: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def is_closed(self) -> bool:
        return self._closed

    def reset(self) -> None:
        self._closed = False
        self._paused = False
        self._gate.set()
        # 清空残留控制事件
        while not self.bridge.empty():
            try:
                self.bridge.get_nowait()
            except asyncio.QueueEmpty:
                break

    def pause(self) -> bool:
        """进入暂停。返回是否状态发生变化。"""
        if self._closed:
            return False
        if self._paused:
            return False
        self._paused = True
        self._gate.clear()
        self.bridge.put_nowait(("paused", None))
        logger.info("分析已暂停 session=%s", self.session_id)
        return True

    def resume(self) -> bool:
        """解除暂停。返回是否状态发生变化。"""
        if self._closed:
            return False
        if not self._paused:
            return False
        self._paused = False
        self._gate.set()
        self.bridge.put_nowait(("resumed", None))
        logger.info("分析已继续 session=%s", self.session_id)
        return True

    async def wait_while_paused(self) -> None:
        """领题前等待；暂停期间阻塞，关闭后立即返回。"""
        while self._paused and not self._closed:
            await self._gate.wait()

    def close(self) -> None:
        self._closed = True
        self._paused = False
        self._gate.set()


_controllers: dict[str, AnalysisController] = {}
_lock = asyncio.Lock()


async def acquire_controller(session_id: str) -> AnalysisController:
    async with _lock:
        ctrl = _controllers.get(session_id)
        if ctrl is None or ctrl.is_closed:
            ctrl = AnalysisController(session_id)
            _controllers[session_id] = ctrl
        else:
            ctrl.reset()
        return ctrl


def get_controller(session_id: str) -> Optional[AnalysisController]:
    ctrl = _controllers.get(session_id)
    if ctrl is None or ctrl.is_closed:
        return None
    return ctrl


async def release_controller(session_id: str) -> None:
    async with _lock:
        ctrl = _controllers.pop(session_id, None)
        if ctrl:
            ctrl.close()
