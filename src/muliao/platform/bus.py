"""进程内事件总线，把任务状态与运行中事件推给前端 SSE（不关心事件是什么，只要求是带 type 的模型）。

只用于展示加速：数据库是唯一事实源。订阅者跟不上时直接断开它，前端重连后从数据库重新拉取。服务关停时关闭总线，
结束所有订阅，SSE 连接随之正常结束，不必等 HTTP 服务器到时强行取消（#330）。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Literal

from pydantic import BaseModel

from muliao.platform.tables import TaskStatus

log = logging.getLogger(__name__)

# 总线上的事件：各发布方定义的 SSE 模型（心跳由 SSE 接口自己发，不经过总线），都带 type
Event = BaseModel


class TaskEvent(BaseModel):
    """一个 task 的状态变化（前端据此从数据库重新拉取），由 worker 发出。"""

    type: Literal["task"] = "task"
    task_id: int
    kind: str
    status: TaskStatus
    session_id: str | None
    # 系统通知的投递没有所属 agent
    agent: str | None
    error: str | None


class SubscriptionEnded(Exception):
    """订阅已被总线结束（跟不上被断开，或总线已关闭），原因在异常文本里。"""


class _End:
    def __init__(self, reason: str):
        self.reason = reason


class Subscriber:
    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue[Event | _End] = asyncio.Queue(maxsize)

    async def next(self) -> Event:
        """取下一个事件。可以安全地被超时取消（只取消这一次读取，订阅本身不受影响）。"""
        event = await self.queue.get()
        if isinstance(event, _End):
            raise SubscriptionEnded(event.reason)
        return event

    def end(self, reason: str) -> None:
        """放入结束标记：之前的事件照常取完，之后 next 抛 SubscriptionEnded。队列满时腾出一个位置放它。"""
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(_End(reason))

    async def __aiter__(self) -> AsyncIterator[Event]:
        # 注意：不要对这个异步生成器套超时——取消会终结生成器。需要超时时用 next()
        while True:
            yield await self.next()


_CLOSED = "event bus closed: the service is shutting down"


class EventBus:
    def __init__(self, subscriber_buffer: int = 1000):
        self._subscribers: set[Subscriber] = set()
        self._buffer = subscriber_buffer
        self._closed = False

    def publish(self, event: Event) -> None:
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning("event subscriber overflowed, disconnecting it", extra={"event_type": event.type})
                self._subscribers.discard(sub)
                sub.end("event subscriber fell behind and was disconnected")

    def close(self) -> None:
        """服务关停：结束现有的订阅，之后的订阅一开始就结束。之后发布的事件没有人收（关停时任务状态仍会变化）。"""
        self._closed = True
        for sub in self._subscribers:
            sub.end(_CLOSED)
        self._subscribers.clear()

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[Subscriber]:
        sub = Subscriber(self._buffer)
        if self._closed:
            sub.end(_CLOSED)
        else:
            self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)


class EventFeed:
    """总线的订阅一面（HTTP 层的 SSE 用），转发给 EventBus。HTTP 层拿到的是这个对象而不是总线本身，所以在运行时
    也发布不了事件。"""

    def __init__(self, bus: EventBus):
        self._bus = bus

    def subscribe(self) -> AbstractAsyncContextManager[Subscriber]:
        return self._bus.subscribe()
