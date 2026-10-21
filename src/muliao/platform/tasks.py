"""持久化任务队列。

所有工作（会话的一次运行、压缩、反思、投递）都是 task。语义：
- 同一 lane 内严格 FIFO、最多一个 running；不同 lane 并发。
- 契约内的暂时性失败按指数退避重试，超过上限置 failed；其他失败直接 failed。
- 进程崩溃遗留的 running 在启动时复位为 pending（见 docs/architecture.md「用最终正确性换架构简化」）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database, utcnow
from muliao.platform.tables import Task, TaskKind, TaskStatus

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# handler 完成工作后返回的提交函数：在标记 task done 的同一事务里写入结果
Commit = Callable[[AsyncSession], Awaitable[None]]
# task 最终失败（不再重试）时，在记下失败的同一事务里由 handler 收尾（如把失败交回等着结果的会话）
Settle = Callable[[AsyncSession, Task], Awaitable[None]]


async def no_commit(_s: AsyncSession) -> None:
    """handler 没有需要与「标记 done」一起提交的结果时返回它。"""


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


class TaskQueue:
    def __init__(self, db: Database, *, max_attempts: int, retry_base_seconds: float, clock: Clock = utcnow):
        self._db = db
        self._max_attempts = max_attempts
        self._retry_base = retry_base_seconds
        self._clock = clock
        self.wake = asyncio.Event()

    def now(self) -> datetime:
        return self._clock()

    @property
    def max_attempts(self) -> int:
        """尝试次数的上限：重试与启动恢复共用（Worker._recover）。"""
        return self._max_attempts

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

    async def claim(self, kinds: Collection[TaskKind]) -> Task | None:
        """领取一个可以执行的 task，只在调用方能处理的这些种类里找。"""
        return await self._claim(_claimable().where(Task.kind.in_(list(kinds))).order_by(Task.id).limit(1))

    async def claim_task(self, task_id: int, kinds: Collection[TaskKind]) -> Task | None:
        """现在就领取这一个 task（Worker.run_now）；它此刻不可领取（已被领取、同 lane 有更早或进行中的、还没到时间、
        不是这些种类）时为 None，留给 worker。"""
        return await self._claim(_claimable().where(Task.id == task_id, Task.kind.in_(list(kinds))))

    async def unclaim(self, task_id: int) -> bool:
        """把一个还在 running 的 task 放回 pending（领取它的执行者被取消、不会再完成它，同 recover 的语义：已用的
        尝试次数不退回）。已经完成或记下失败的不动。返回是否放回了。"""
        async with self._db.transaction() as s:
            task = await s.get(Task, task_id)
            if task is None or task.status != TaskStatus.RUNNING:
                return False
            task.status = TaskStatus.PENDING
        self.wake.set()
        return True

    async def _claim(self, query: Select[tuple[Task]]) -> Task | None:
        now = self._clock()
        async with self._db.transaction() as s:  # 事务串行化保证同一 task 不会被领取两次
            task = await s.scalar(query.where(Task.not_before <= now))
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
        """启动时调用：上次进程遗留的 running 复位为 pending，返回被恢复的 task。尝试次数是否已经用完由
        调用方判断（Worker._recover 把用完的记为最终失败，不再恢复）。"""
        async with self._db.transaction() as s:
            tasks = list((await s.scalars(select(Task).where(Task.status == TaskStatus.RUNNING))).all())
            for task in tasks:
                task.status = TaskStatus.PENDING
            return tasks

    async def retry(self, task_id: int) -> Task:
        """人工重试一个 failed task，沿用原来的 id（同 lane 内按 id FIFO）。

        会话等 lane 上还有未结束的 task 时拒绝：重试会插到更新的工作前面，和正在进行的运行争同一个会话。
        投递任务（EVENT）不受这个限制：共享的订阅 lane 上可能一直有新的投递，等它空下来可能永远等不到。沿用原 id
        只保证它排在还没处理的投递前面；在它失败之后已经处理过的投递不会重来，所以依赖顺序的处理器遇到前置状态
        缺失时必须自行判断（跳过并记日志，或在确属契约违约时报错），重试按 id 先后进行。
        """
        async with self._db.transaction() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise NotFound(f"task {task_id} not found")
            if task.status != TaskStatus.FAILED:
                raise InvalidInput(f"only failed tasks can be retried, task {task_id} is {task.status}")
            if task.kind != TaskKind.EVENT and await self.lane_busy(s, task.lane):
                raise Conflict(
                    f"lane {task.lane} still has unfinished tasks; retry task {task_id} after they finish"
                )
            task.status = TaskStatus.PENDING
            task.attempts = 0
            task.not_before = self._clock()
            task.finished_at = None
            event.listen(s.sync_session, "after_commit", lambda _s: self.wake.set(), once=True)
            return task

    async def next_due(self, kinds: Collection[TaskKind]) -> datetime | None:
        """最早一个「可领取、只是还没到时间」的 task 的时间；被同 lane 挡住的 task 由 complete/fail 唤醒。"""
        claimable = _claimable().where(Task.kind.in_(list(kinds))).subquery()
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
