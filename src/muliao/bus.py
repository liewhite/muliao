"""进程内事件总线，把运行中事件推给前端 SSE。

只用于展示加速：数据库是唯一事实源。订阅者跟不上时直接断开它，前端重连后从数据库重新拉取。
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

Event = dict[str, Any]


def task_event(
    *,
    task_id: int | None,
    kind: str,
    status: str,
    session_id: str | None,
    agent_id: str | None,
    error: str | None,
) -> Event:
    """一个 task 的状态变化（前端据此从数据库重新拉取）。worker 执行的 task 与内联跑完的私有 thread 轮次共用这一个形状；
    内联轮次失败时没有 task 记录（失败不入库），task_id 为 None。"""
    return {
        "type": "task",
        "task_id": task_id,
        "kind": kind,
        "status": status,
        "session_id": session_id,
        "agent": agent_id,
        "error": error,
    }


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
                log.warning(
                    "event subscriber overflowed, disconnecting it", extra={"event_type": event.get("type")}
                )
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


@dataclass(frozen=True)
class LiveRun:
    run_id: str
    events: list[Event]


class LiveRuns:
    """每个会话正在进行的那一轮的运行事件，在内存里留一份，让中途打开会话页的人从这一轮的开头看起。

    和事件总线一样只是展示加速：一轮提交（或失败）后由数据库接管（end），进程重启时这一轮本来也会重跑，缓存丢了无妨。
    事件带上这一轮的 run_id 与递增的 seq，前端用它们把快照与之后经 SSE 收到的事件合并，并发现两者之间的缺口。
    没有会话的运行（反思、临时 agent）不缓存，照常推给总线。生命周期只由 TurnRunner 管（begin / end）。
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._runs: dict[str, LiveRun] = {}

    def begin(self, session_id: str) -> None:
        if session_id in self._runs:
            # 同一会话同时只有一轮（lane 串行、内联轮次有锁）；已有记录说明上一轮漏了 end 或这个前提被打破
            raise RuntimeError(f"session {session_id} already has a live run")
        self._runs[session_id] = LiveRun(uuid.uuid4().hex, [])

    def end(self, session_id: str) -> None:
        self._runs.pop(session_id, None)

    def publish(self, session_id: str | None, event: Event) -> None:
        if session_id is not None:
            run = self._runs.get(session_id)
            if run is None:
                # 会话的每一轮都由 TurnRunner 先 begin；没有说明调用方漏了这一步
                raise RuntimeError(f"run event for session {session_id} outside a begun run")
            event = {**event, "run_id": run.run_id, "seq": len(run.events)}
            run.events.append(event)
        self._bus.publish(event)

    def snapshot(self, session_id: str) -> LiveRun | None:
        run = self._runs.get(session_id)
        return None if run is None else LiveRun(run.run_id, list(run.events))
