"""用户停止与关停、总时限的区分。停止之后要做的事在 runtime.steps（save_stopped）与 runtime.halt（SessionHalt），设计见
docs/architecture.md「停止会话」。

运行放在自己的原生任务里，用户停止只取消这个任务；外层（worker 的任务组、fail_after）被取消时，宿主在等待处收到取消，
再取消运行的任务后照原语义抛出。用 asyncio.wait 等运行结束：它在运行被取消时不抛出，只在宿主自己被取消时抛出，
所以「运行被取消而宿主没有」就是用户停止，不需要数取消次数。

原生取消只在没有任何数据库事务进行时发出（cancel_gate，即 db.Database.exclusive）：屏蔽挡不住原生取消，它打断进行中
的语句会让连接作废、留下等不到的关闭任务。在这个时刻发出，运行树里的任务只会在等锁处收到取消。

每次运行最多被原生取消一次（重复停止、停止后又赶上关停都不再取消）：原生取消是边沿触发的，屏蔽作用域挡不住对同一个
任务的第二次 cancel()，运行里屏蔽着的清理（删掉没起来的后台任务、等 git 退出……）会被它打断。

这里是并发规则（只用 anyio）的唯一例外，依据：pydantic-ai 把每个工具调用放在原生任务里并 await 它。被取消的任务会把
取消连同消息传给它正在 await 的任务（asyncio 的行为），用 anyio 的取消作用域停止运行时，anyio 的取消消息就这样传进
工具的任务，工具里的 anyio 任务组（如 shell）会把它当成自己的取消吞掉，工具照常返回结果（实测：被停止的命令得到
「超时被杀」的结果）。用不带消息的原生取消停止运行，工具里的 anyio 作用域会如实向上抛出。
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

import anyio

log = logging.getLogger(__name__)

# 外层取消后等运行收尾的时限
_WIND_DOWN_SECONDS = 10.0


@dataclass(frozen=True)
class Stopped:
    """这次运行被用户停止了（StoppableRuns.run 的结果之一）。"""


class StoppableRuns[T]:
    """按会话登记正在进行的运行，用户可以停止它。同一会话同时只有一次运行（会话的 lane 串行）。"""

    def __init__(self, cancel_gate: Callable[[], AbstractAsyncContextManager[None]]):
        self._running: dict[str, asyncio.Task[T]] = {}
        # 已经取消过的运行（每个最多取消一次）
        self._cancelled: set[asyncio.Task[T]] = set()
        self._gate = cancel_gate

    async def _cancel(self, inner: asyncio.Task[T]) -> None:
        if inner in self._cancelled:
            return
        self._cancelled.add(inner)
        async with self._gate():
            inner.cancel()

    async def run(self, session_id: str, start: Callable[[], Coroutine[Any, Any, T]]) -> T | Stopped:
        if session_id in self._running:
            # 前提被打破时停止会停错一次运行：在第一现场报出
            raise RuntimeError(f"session {session_id} already has a running run")
        inner = asyncio.create_task(start(), name=f"run-{session_id}")  # noqa: TID251  见模块说明
        self._running[session_id] = inner
        try:
            try:
                await asyncio.wait({inner})  # noqa: TID251  运行被取消时不抛出，宿主被取消时才抛出
            except asyncio.CancelledError:
                # 外层被取消（关停、总时限）：这次运行一起取消（不带消息，见模块说明），按原来的语义抛出。
                # 外层的取消是电平触发的：取消与等运行收尾都要屏蔽它，并设时限，收尾卡住也不拖住关停
                with anyio.CancelScope(shield=True), anyio.move_on_after(_WIND_DOWN_SECONDS):
                    await self._cancel(inner)
                    await asyncio.wait({inner})  # noqa: TID251
                if not inner.done():
                    # 运行吞掉了取消：这是 bug。它保持登记到真正结束为止，同一会话的下一次运行在入口报错，不会有两个
                    # 运行同时操作这个会话
                    log.error(
                        "run did not wind down after cancellation; it stays registered until it ends",
                        extra={"session_id": session_id},
                    )
                    inner.add_done_callback(lambda done: self._late_end(session_id, done))
                elif not inner.cancelled() and (error := inner.exception()) is not None:
                    log.warning(
                        "run failed while being cancelled: %r", error, extra={"session_id": session_id}
                    )
                raise
            if inner.cancelled():
                return Stopped()
            return inner.result()
        finally:
            if inner.done() and self._running.get(session_id) is inner:
                del self._running[session_id]
                self._cancelled.discard(inner)

    def _late_end(self, session_id: str, inner: asyncio.Task[T]) -> None:
        """收尾超时的运行最终结束了：撤销登记，取走它的结局记下来。"""
        if self._running.get(session_id) is inner:
            del self._running[session_id]
        self._cancelled.discard(inner)
        error = None if inner.cancelled() else inner.exception()
        log.warning(
            "run that overran its wind-down has ended", extra={"session_id": session_id, "error": repr(error)}
        )

    async def stop(self, session_id: str) -> bool:
        """停止这个会话正在进行的运行；没有在运行的返回 False。"""
        inner = self._running.get(session_id)
        if inner is None:
            return False
        await self._cancel(inner)
        return True
