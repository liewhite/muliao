"""用户停止与关停、总时限的区分。停止之后要做的事在 runtime.steps（save_stopped）与 runtime.halt（SessionHalt），设计见
docs/architecture.md「停止会话」。

运行放在单独的子任务里，用户停止只取消它；worker 的任务没被取消，所以「子任务被取消、外层没被取消」就是用户停止，
其余的取消（关停、总时限取消的是外层任务）一起取消子任务后照原语义抛出，已保存的步骤留着，由队列恢复或重试续跑。
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stopped:
    """这次运行被用户停止了（StoppableRuns.run 的结果之一）。"""


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class StoppableRuns[T]:
    """按会话登记正在进行的运行，用户可以停止它。同一会话同时只有一次运行（会话的 lane 串行）。"""

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[T]] = {}

    async def run(self, session_id: str, start: Callable[[], Coroutine[Any, Any, T]]) -> T | Stopped:
        if session_id in self._running:
            # 前提被打破时停止会停错一轮：在第一现场报出
            raise RuntimeError(f"session {session_id} already has a running run")
        inner = asyncio.create_task(start(), name=f"run-{session_id}")
        self._running[session_id] = inner
        try:
            return await inner
        except asyncio.CancelledError:
            if inner.cancelled() and not _cancelling():
                return Stopped()
            # 外层被取消（关停、总时限）：这次运行一起取消，按原来的语义抛出
            inner.cancel()
            await asyncio.wait({inner})
            if not inner.cancelled() and (error := inner.exception()) is not None:
                log.warning("run failed while being cancelled: %r", error, extra={"session_id": session_id})
            raise
        finally:
            if self._running.get(session_id) is inner:
                del self._running[session_id]

    def stop(self, session_id: str) -> bool:
        """停止这个会话正在进行的运行；没有在运行的返回 False。"""
        inner = self._running.get(session_id)
        if inner is None:
            return False
        inner.cancel()
        return True
