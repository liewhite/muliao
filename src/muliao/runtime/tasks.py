"""持久化任务队列。

所有工作（会话的一次运行、压缩、反思、投递）都是 task。语义：
- 同一 lane 内严格 FIFO、最多一个 running；不同 lane 并发。
- 契约内的暂时性失败按指数退避重试，超过上限置 failed；其他失败直接 failed。
- 进程崩溃遗留的 running 在启动时复位为 pending（见 docs/architecture.md「用最终正确性换架构简化」）。
- 不经 worker 执行的运行（ask_agent 在发问方那一步里内联驱动私有 thread）用 start_inline 直接记为 running，
  保持「每一次运行都是一个 task」；它们不能被重启后恢复，recover 记为失败。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from muliao.db import Database, utcnow
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.models import Task, TaskKind, TaskStatus

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# handler 完成工作后返回的提交函数：在标记 task done 的同一事务里写入结果
Commit = Callable[[AsyncSession], Awaitable[None]]
# task 最终失败（不再重试）时，在记下失败的同一事务里由 handler 收尾（如把失败交回等着结果的会话）
Settle = Callable[[AsyncSession, Task], Awaitable[None]]


async def no_commit(_s: AsyncSession) -> None:
    """handler 没有需要与「标记 done」一起提交的结果时返回它。"""


def turn_lane(session_id: str) -> str:
    return f"session:{session_id}"


_INLINE = "inline:"


def inline_lane(session_id: str) -> str:
    """私有 thread 的运行：在发问方那一步里内联执行，不经过队列领取（TaskQueue.start_inline）。"""
    return f"{_INLINE}{session_id}"


def reflection_lane(agent_id: str) -> str:
    return f"reflection:{agent_id}"


def deliver_lane(session_id: str | None, agent_id: str | None) -> str:
    return f"deliver:{session_id or agent_id or 'system'}"


def _claimable():  # noqa: ANN202
    """可领取的 pending：它是所在 lane 最早的 pending，且该 lane 没有 running。claim 与 next_due 共用。"""
    running = aliased(Task)
    earlier = aliased(Task)
    return (
        select(Task)
        .where(Task.status == TaskStatus.PENDING)
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
    )


async def running_task_ids(
    s: AsyncSession, *, session_id: str | None = None, agent_id: str | None = None
) -> list[int]:
    """正在运行的 task id（包括私有 thread 的内联运行，见 TaskQueue.start_inline）。删除 session/agent 前用它保证
    不会删掉执行中的工作。"""
    query = select(Task.id).where(Task.status == TaskStatus.RUNNING)
    if session_id is not None:
        query = query.where(Task.session_id == session_id)
    if agent_id is not None:
        query = query.where(Task.agent_id == agent_id)
    return list((await s.scalars(query)).all())


class TaskQueue:
    def __init__(self, db: Database, *, max_attempts: int, retry_base_seconds: float, clock: Clock = utcnow):
        self._db = db
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._clock = clock
        self.wake = asyncio.Event()

    def now(self) -> datetime:
        return self._clock()

    def enqueue(
        self,
        s: AsyncSession,
        *,
        kind: TaskKind,
        lane: str,
        agent_id: str | None,
        session_id: str | None,
        payload: dict[str, Any],
    ) -> Task:
        """在调用方事务内写入 task；事务提交后唤醒 worker。"""
        task = Task(
            kind=kind,
            lane=lane,
            agent_id=agent_id,
            session_id=session_id,
            payload=payload,
            status=TaskStatus.PENDING,
            not_before=self._clock(),
        )
        s.add(task)
        event.listen(s.sync_session, "after_commit", lambda _s: self.wake.set(), once=True)
        return task

    def start_inline(self, s: AsyncSession, *, agent_id: str, session_id: str) -> Task:
        """在调用方事务内记下一次在别处（发问方那一步里）执行的运行，从一开始就是 running、不会被领取；
        结束时照常 complete 或 fail。它的 lane 不参与排队（inline_lane）。"""
        now = self._clock()
        task = Task(
            kind=TaskKind.TURN,
            lane=inline_lane(session_id),
            agent_id=agent_id,
            session_id=session_id,
            payload={},
            status=TaskStatus.RUNNING,
            attempts=1,
            not_before=now,
            started_at=now,
        )
        s.add(task)
        return task

    async def claim(self) -> Task | None:
        now = self._clock()
        query = _claimable().where(Task.not_before <= now).order_by(Task.id).limit(1)
        async with self._db.transaction() as s:  # 事务串行化保证同一 task 不会被领取两次
            task = await s.scalar(query)
            if task is None:
                return None
            task.status = TaskStatus.RUNNING
            task.attempts += 1
            task.started_at = now
            # 保留上一次失败的 error，运行期间 UI 仍能看到为什么在重试
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
            task.error = None
            task.finished_at = self._clock()
        # 同 lane 的后续 task 现在可以领取了
        self.wake.set()

    async def fail(self, task_id: int, error: str, *, retryable: bool, on_final: Settle) -> Task:
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
                await on_final(s, task)
        # 重试时间点或同 lane 的后续 task 需要重新计算等待
        self.wake.set()
        return task

    async def finish_pending_in(self, s: AsyncSession, lane: str, kind: TaskKind) -> list[Task]:
        """在调用方事务里把这个 lane 上排队（含等重试）的这种 task 记为完成，不再执行（用户停止会话）。"""
        tasks = list(
            (
                await s.scalars(
                    select(Task).where(
                        Task.lane == lane, Task.kind == kind, Task.status == TaskStatus.PENDING
                    )
                )
            ).all()
        )
        for task in tasks:
            task.status, task.finished_at = TaskStatus.DONE, self._clock()
        return tasks

    async def recover(self) -> list[Task]:
        """启动时调用：上次进程遗留的 running 复位为 pending，返回被恢复的 task。

        内联运行（私有 thread）不复位：发问方那次运行续跑时会重新发问，交给 worker 接手会让同一个 thread 同时有两个
        消费者。记为失败。"""
        async with self._db.transaction() as s:
            tasks = list((await s.scalars(select(Task).where(Task.status == TaskStatus.RUNNING))).all())
            for task in tasks:
                if task.lane.startswith(_INLINE):
                    task.status, task.finished_at = TaskStatus.FAILED, self._clock()
                    task.error = "interrupted by a restart; the asking agent asks again when its run resumes"
                else:
                    task.status = TaskStatus.PENDING
            return [t for t in tasks if t.status == TaskStatus.PENDING]

    async def retry(self, task_id: int) -> Task:
        """人工重试一个 failed task。

        同 lane 还有未结束的 task 时拒绝：重试沿用原来的 id，会按 FIFO 插到更新的工作前面，和正在进行的运行
        争同一个会话。
        """
        async with self._db.transaction() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise NotFound(f"task {task_id} not found")
            if task.status != TaskStatus.FAILED:
                raise InvalidInput(f"only failed tasks can be retried, task {task_id} is {task.status}")
            if await self.lane_busy(s, task.lane):
                raise Conflict(
                    f"lane {task.lane} still has unfinished tasks; retry task {task_id} after they finish"
                )
            task.status = TaskStatus.PENDING
            task.attempts = 0
            task.not_before = self._clock()
            task.finished_at = None
            event.listen(s.sync_session, "after_commit", lambda _s: self.wake.set(), once=True)
            return task

    async def next_due(self) -> datetime | None:
        """最早一个「可领取、只是还没到时间」的 task 的时间；被同 lane 挡住的 task 由 complete/fail 唤醒。"""
        claimable = _claimable().subquery()
        async with self._db.transaction() as s:
            return await s.scalar(select(func.min(claimable.c.not_before)))

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
