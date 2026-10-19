"""持久化任务队列。

所有工作（对话一轮、反思、投递）都是 task。语义：
- 同一 lane 内严格 FIFO、最多一个 running；不同 lane 并发。
- 契约内的暂时性失败按指数退避重试，超过上限置 failed；其他失败直接 failed。
- 进程崩溃遗留的 running 在启动时复位为 pending（见 docs/architecture.md「用最终正确性换架构简化」）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from muliao.db import Database, utcnow
from muliao.errors import InvalidInput, NotFound
from muliao.models import Task, TaskKind, TaskStatus

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# handler 完成工作后返回的提交函数：在标记 task done 的同一事务里写入结果
Commit = Callable[[AsyncSession], Awaitable[None]]


def turn_lane(session_id: str) -> str:
    return f"session:{session_id}"


def reflection_lane(agent_name: str) -> str:
    return f"reflection:{agent_name}"


def deliver_lane(session_id: str | None, agent_name: str) -> str:
    return f"deliver:{session_id or agent_name}"


class TaskQueue:
    def __init__(self, db: Database, *, max_attempts: int, retry_base_seconds: float, clock: Clock = utcnow):
        self._db = db
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._clock = clock
        self.wake = asyncio.Event()

    def enqueue(
        self,
        s: AsyncSession,
        *,
        kind: TaskKind,
        lane: str,
        agent_name: str,
        session_id: str | None,
        payload: dict[str, Any],
    ) -> Task:
        """在调用方事务内写入 task；事务提交后唤醒 worker。"""
        task = Task(
            kind=kind,
            lane=lane,
            agent_name=agent_name,
            session_id=session_id,
            payload=payload,
            status=TaskStatus.PENDING,
            not_before=self._clock(),
        )
        s.add(task)
        event.listen(s.sync_session, "after_commit", lambda _s: self.wake.set(), once=True)
        return task

    async def claim(self) -> Task | None:
        """领取一个可执行 task：它是所在 lane 最早的 pending，且该 lane 没有 running。"""
        now = self._clock()
        running = aliased(Task)
        earlier = aliased(Task)
        query = (
            select(Task)
            .where(Task.status == TaskStatus.PENDING, Task.not_before <= now)
            .where(
                ~select(running.id)
                .where(running.lane == Task.lane, running.status == TaskStatus.RUNNING)
                .exists()
            )
            .where(
                ~select(earlier.id)
                .where(earlier.lane == Task.lane, earlier.status == TaskStatus.PENDING, earlier.id < Task.id)
                .exists()
            )
            .order_by(Task.id)
            .limit(1)
        )
        async with self._db.transaction() as s:  # 事务串行化保证同一 task 不会被领取两次
            task = await s.scalar(query)
            if task is None:
                return None
            task.status = TaskStatus.RUNNING
            task.attempts += 1
            task.started_at = now
            task.error = None
            return task

    async def complete(self, task_id: int, commit: Commit) -> None:
        async with self._db.transaction() as s:
            task = await s.get(Task, task_id)
            if task is None or task.status != TaskStatus.RUNNING:
                raise RuntimeError(
                    f"task {task_id} is not running, cannot complete it (status={task and task.status})"
                )
            await commit(s)
            task.status = TaskStatus.DONE
            task.finished_at = self._clock()
        # 同 lane 的后续 task 现在可以领取了
        self.wake.set()

    async def fail(self, task_id: int, error: str, *, retryable: bool) -> Task:
        async with self._db.transaction() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise RuntimeError(f"task {task_id} vanished while failing it")
            task.error = error
            if retryable and task.attempts < self._max_attempts:
                delay = self._retry_base * 2 ** (task.attempts - 1)
                task.status = TaskStatus.PENDING
                task.not_before = self._clock() + timedelta(seconds=delay)
            else:
                task.status = TaskStatus.FAILED
                task.finished_at = self._clock()
        # 重试时间点或同 lane 的后续 task 需要重新计算等待
        self.wake.set()
        return task

    async def recover(self) -> int:
        """启动时调用：上次进程遗留的 running 复位为 pending。"""
        async with self._db.transaction() as s:
            result = await s.execute(
                update(Task).where(Task.status == TaskStatus.RUNNING).values(status=TaskStatus.PENDING)
            )
            return result.rowcount

    async def retry(self, task_id: int) -> Task:
        """人工重试一个 failed task。"""
        async with self._db.transaction() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise NotFound(f"task {task_id} not found")
            if task.status != TaskStatus.FAILED:
                raise InvalidInput(f"only failed tasks can be retried, task {task_id} is {task.status}")
            task.status = TaskStatus.PENDING
            task.attempts = 0
            task.not_before = self._clock()
            task.finished_at = None
            event.listen(s.sync_session, "after_commit", lambda _s: self.wake.set(), once=True)
            return task

    async def next_due(self) -> datetime | None:
        async with self._db.transaction() as s:
            return await s.scalar(
                select(Task.not_before)
                .where(Task.status == TaskStatus.PENDING)
                .order_by(Task.not_before)
                .limit(1)
            )

    async def list(self, *, status: TaskStatus | None, session_id: str | None, limit: int) -> list[Task]:
        query = select(Task).order_by(Task.id.desc()).limit(limit)
        if status is not None:
            query = query.where(Task.status == status)
        if session_id is not None:
            query = query.where(Task.session_id == session_id)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    async def lane_busy(self, s: AsyncSession, lane: str) -> bool:
        busy = await s.scalar(
            select(Task.id)
            .where(Task.lane == lane, Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
            .limit(1)
        )
        return busy is not None
