"""领域事件（docs/architecture.md 2.2）：一个领域宣布自己发生了什么，不知道谁在听；订阅方在组合根登记。

发布就是在发布方的事务里按登记顺序逐个调用订阅者：订阅者的写入与发布方的状态变化一起提交，任何一个出错整个事务回滚、
错误交给发布方的调用方。所以订阅者只做短的数据库操作：事务外的事（杀进程、删文件）用 db.when_committed_in 排到提交之后，
要异步、要重试的事由订阅者登记自己领域的任务。

一个领域请另一个指定的领域做事不用事件，直接调用对方的接口（同一个事务时把 s 传过去）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession


class DomainEvent(BaseModel):
    """一个领域宣布自己发生了什么（事件的数据）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")


type Subscriber[E: DomainEvent] = Callable[[AsyncSession, E], Awaitable[None]]


def _concrete(base: type[DomainEvent]) -> list[type[DomainEvent]]:
    """muliao 包里 base 的全部子类。测试里定义的事件不算：它们不经组合根登记。"""
    found, todo = [], list(base.__subclasses__())
    while todo:
        cls = todo.pop()
        todo += cls.__subclasses__()
        if cls.__module__.startswith("muliao."):
            found.append(cls)
    return found


class Messaging:
    def __init__(self) -> None:
        self._subscribers: dict[type[DomainEvent], list[Subscriber[Any]]] = {}

    def subscribe[E: DomainEvent](self, event_type: type[E], subscriber: Subscriber[E]) -> None:
        """登记（组合根，启动前）。同一事件的订阅者按登记顺序调用。"""
        self._subscribers.setdefault(event_type, []).append(subscriber)

    def check_wired(self) -> None:
        """装配结束时调用一次：代码里（muliao 包下）定义的每个事件都至少有一个订阅者，缺了当场报错。发布方不认识
        订阅方，漏登记时 publish_in 什么都不做，这里把它提前到启动。"""
        missing = [
            f"{cls.__module__}.{cls.__qualname__}"
            for cls in _concrete(DomainEvent)
            if cls not in self._subscribers
        ]
        if missing:
            raise RuntimeError(f"events with no subscriber registered: {', '.join(sorted(missing))}")

    async def publish_in(self, s: AsyncSession, event: DomainEvent) -> None:
        """在发布方的事务里调用每个订阅者。"""
        for subscriber in self._subscribers.get(type(event), []):
            await subscriber(s, event)
