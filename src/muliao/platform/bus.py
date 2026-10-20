"""进程内事件总线，把任务状态与运行中事件推给前端 SSE（不关心事件是什么，只要求是带 type 的模型）。

只用于展示加速：数据库是唯一事实源。订阅者跟不上时直接断开它，前端重连后从数据库重新拉取。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from pydantic import BaseModel

from muliao.platform.tables import TaskKind, TaskStatus

log = logging.getLogger(__name__)

# 总线上的事件：各发布方定义的 SSE 模型（心跳由 SSE 接口自己发，不经过总线），都带 type
Event = BaseModel


class TaskEvent(BaseModel):
    """一个 task 的状态变化（前端据此从数据库重新拉取），由 worker 发出。"""

    type: Literal["task"] = "task"
    task_id: int
    kind: TaskKind
    status: TaskStatus
    session_id: str | None
    # 系统通知的投递没有所属 agent
    agent: str | None
    error: str | None


class SubscriberOverflow(Exception):
    pass


class _Subscriber:
    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize)
        self.overflowed = False

    async def next(self) -> Event:
        """取下一个事件。可以安全地被超时取消（只取消这一次读取，订阅本身不受影响）。"""
        event = await self.queue.get()
        if event is None:
            raise SubscriberOverflow("event subscriber fell behind and was disconnected")
        return event

    async def __aiter__(self) -> AsyncIterator[Event]:
        # 注意：不要对这个异步生成器套超时——取消会终结生成器。需要超时时用 next()
        while True:
            yield await self.next()


class EventBus:
    def __init__(self, subscriber_buffer: int = 1000):
        self._subscribers: set[_Subscriber] = set()
        self._buffer = subscriber_buffer

    def publish(self, event: Event) -> None:
        for sub in list(self._subscribers):
            if sub.overflowed:
                continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event subscriber overflowed, disconnecting it", extra={"event_type": event.type})
                sub.overflowed = True
                self._subscribers.discard(sub)
                # 腾出一个位置放结束标记
                sub.queue.get_nowait()
                sub.queue.put_nowait(None)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[_Subscriber]:
        sub = _Subscriber(self._buffer)
        self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)
