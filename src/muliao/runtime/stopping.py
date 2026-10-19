"""用户停止一轮：停止的语义（与关停、总时限区分）和停止后要保存的消息都在这里。设计见 docs/architecture.md「停止一轮」。

模型运行放在单独的子任务里，用户停止只取消它；worker 的任务没被取消，所以「子任务被取消、外层没被取消」就是用户停止，
其余的取消（关停、总时限取消的是外层任务）一起取消子任务后照原语义抛出，这一轮不留记录，由队列恢复或重试。
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserContent, UserPromptPart

from muliao.conventions import STOPPED_TOOL_RESULT, TURN_STOPPED, unanswered_calls

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stopped:
    """这次运行被用户停止了（StoppableRuns.run 的结果之一）。"""


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


class StoppableRuns[T]:
    """按会话登记正在进行的运行，用户可以停止它。同一会话同时只有一次运行（lane 串行、内联轮次有锁）。"""

    def __init__(self) -> None:
        self._running: dict[str, asyncio.Task[T]] = {}

    async def run(self, session_id: str, start: Callable[[], Coroutine[Any, Any, T]]) -> T | Stopped:
        if session_id in self._running:
            # 前提被打破时停止会停错一轮：在第一现场报出
            raise RuntimeError(f"session {session_id} already has a running turn")
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


def stopped_messages(
    new: Sequence[ModelMessage] | None, user_prompt: str | Sequence[UserContent] | None
) -> list[ModelMessage]:
    """用户停止的一轮要保存的消息：停止前已经产生的部分（工具已经执行过，下一轮的模型要知道），没出结果的调用补上
    结果，最后一条标上 TURN_STOPPED。正在生成的那段回答由 pydantic-ai 以 state='interrupted' 留在运行历史里，一起保存，
    其中参数还没生成完的工具调用同样补上结果（参数在落库前脱敏，截断的 vault_set 参数整体替换，见 redaction）。
    new 为 None 表示还没开始第一次模型请求，只有用户这条消息。"""
    if new is None:
        new = [] if user_prompt is None else [ModelRequest(parts=[UserPromptPart(user_prompt)])]
    messages = list(new)
    if dangling := unanswered_calls(messages):
        messages.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(c.tool_name, STOPPED_TOOL_RESULT, tool_call_id=c.tool_call_id)
                    for c in dangling
                ]
            )
        )
    if messages:
        last = messages[-1]
        messages[-1] = replace(last, metadata={**(last.metadata or {}), TURN_STOPPED: True})
    return messages


def stopped_tool_calls(messages: Sequence[ModelMessage]) -> int:
    """停止时补上结果的调用数（日志用）。"""
    return sum(
        1
        for m in messages
        for p in m.parts
        if isinstance(p, ToolReturnPart) and p.content == STOPPED_TOOL_RESULT
    )
