"""持久化任务队列。

所有工作（会话的一次运行、压缩、投递）都是 task。语义：
- 同一 lane 内严格 FIFO、最多一个 running；不同 lane 并发。
- 契约内的暂时性失败按指数退避重试，超过上限置 failed（模型不可用的上限更长，见 Retries）；其他失败直接 failed。
- 进程崩溃遗留的 running 在启动时复位为 pending（见 docs/architecture.md「用最终正确性换架构简化」）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from muliao.errors import Conflict, InvalidInput, NotFound, ServiceUnavailable, TransientError
from muliao.platform.db import Database, utcnow
from muliao.platform.tables import EVENT_TASK, Task, TaskStatus

log = logging.getLogger(__name__)

Clock = Callable[[], datetime]
# handler 完成工作后返回的提交函数：在标记 task done 的同一事务里写入结果
Commit = Callable[[AsyncSession], Awaitable[None]]
# task 最终失败（不再重试）时，在记下失败的同一事务里由 handler 收尾（如把失败交回等着结果的会话）
Settle = Callable[[AsyncSession, Task], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    """一类暂时性失败的重试：第 n 次尝试失败后等 base·2^(n-1) 秒（封顶 max_delay），用完 max_attempts 次尝试即最终失败。"""

    max_attempts: int
    base_seconds: float
    max_delay_seconds: float

    def delay(self, attempt: int) -> float:
        """第 attempt 次尝试（从 1 起）失败后等多久再试。"""
        return min(self.base_seconds * 2 ** (attempt - 1), self.max_delay_seconds)


@dataclass(frozen=True)
class Retries:
    """哪些失败重试、按什么策略。外部服务不可用（ServiceUnavailable：模型、推送服务）的故障常持续几分钟到半小时，
    用更长的 outage；其余暂时性失败（TransientError、超时）用 transient。别的异常一律按确定性失败记账：平台不认识
    任何 SDK 的异常类型（模型与网络层的由 agent_runtime 换成 ModelUnavailable，推送的由 notifications/webpush 换）。
    尝试次数是两类失败共用的一个计数（task.attempts 不记由哪类用掉）：扛过几次模型故障后再遇到一次普通暂时性失败，
    可能已超过 transient 的上限而直接最终失败。"""

    transient: RetryPolicy
    outage: RetryPolicy

    def for_failure(self, exc: BaseException) -> RetryPolicy | None:
        """这个失败按哪个策略重试；None 表示不重试。"""
        if isinstance(exc, ServiceUnavailable):
            return self.outage
        if isinstance(exc, (TransientError, TimeoutError)):
            return self.transient
        return None

    @property
    def max_attempts(self) -> int:
        """任一策略允许的最多尝试次数（启动恢复用，Worker.recover）。取最大值：正在按 outage 策略重试的任务不能因为
        一次重启就被记为最终失败。"""
        return max(self.transient.max_attempts, self.outage.max_attempts)


@dataclass(frozen=True)
class TaskView:
    """一个任务的只读快照：别的领域经 TaskQueue 的查询看自己 lane 上任务的状态，不查任务表、不按 TaskStatus 分支
    （状态用下面几个判断读）。"""

    id: int
    kind: str
    lane: str
    agent_id: str | None
    session_id: str | None
    payload: Mapping[str, Any]
    status: TaskStatus
    attempts: int
    error: str | None
    not_before: datetime
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def of(cls, task: Task) -> TaskView:
        return cls(
            id=task.id,
            kind=task.kind,
            lane=task.lane,
            agent_id=task.agent_id,
            session_id=task.session_id,
            payload=dict(task.payload),
            status=task.status,
            attempts=task.attempts,
            error=task.error,
            not_before=task.not_before,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )

    @property
    def pending(self) -> bool:
        """排着（含等重试）。"""
        return self.status == TaskStatus.PENDING

    @property
    def running(self) -> bool:
        return self.status == TaskStatus.RUNNING

    @property
    def done(self) -> bool:
        return self.status == TaskStatus.DONE

    @property
    def failed(self) -> bool:
        """最终失败（不再重试）。"""
        return self.status == TaskStatus.FAILED


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


class TaskAdmin:
    """「任务」页的操作：列出与人工重试，转发给 TaskQueue。HTTP 层拿到的是这个对象而不是队列本身，所以在运行时也
    排不了、领取不了、结束不了任务。"""

    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def list(self, *, status: TaskStatus | None, limit: int) -> list[TaskView]:
        return [TaskView.of(t) for t in await self._queue.list(status=status, limit=limit)]

    async def retry(self, task_id: int) -> TaskView:
        return TaskView.of(await self._queue.retry(task_id))


class TaskQueue:
    def __init__(self, db: Database, *, retries: Retries, clock: Clock = utcnow):
        self._db = db
        self.retries = retries
        self._clock = clock
        # 有处理器的任务种类（accept），登记之前不接受任何 task
        self._kinds: frozenset[str] = frozenset()
        self.wake = asyncio.Event()

    def accept(self, kinds: frozenset[str]) -> None:
        """登记有处理器的任务种类（组合根建好 worker 后调用一次）。没有处理器的 task 永远不会被领取，还挡住同一
        lane 后面的 task（_claimable 不看种类），所以 enqueue 当场拒绝别的种类。"""
        if self._kinds:
            raise RuntimeError("task kinds are already registered")
        self._kinds = kinds

    @property
    def kinds(self) -> frozenset[str]:
        """有处理器的任务种类（accept 登记的）。"""
        return self._kinds

    async def check_kinds(self) -> None:
        """启动检查。任务种类是不透明的字符串（platform.tables）：库里还没完成的 task 若是没有处理器登记的种类（例如
        改名后留下的旧值），它永远不会被领取，还挡住它的 lane（停止会话也解不开）。新写入的已由 accept 之后的
        enqueue 拒绝，这里报的是库里留下的旧行。启动时报出来，由人处理：恢复它的处理器，或在库里删掉它；「任务」页上
        它没有可做的操作。"""
        unknown = sorted(await self.unfinished_kinds() - self._kinds)
        if unknown:
            log.error("unfinished tasks of kinds no handler is registered for", extra={"kinds": unknown})

    def now(self) -> datetime:
        return self._clock()

    def _wake_after_commit(self, s: AsyncSession) -> None:
        """这个事务提交后唤醒 worker（新的可领取 task 在提交前对它不可见）。"""
        event.listen(s.sync_session, "after_commit", lambda _s: self.wake.set(), once=True)

    def enqueue(
        self,
        s: AsyncSession,
        *,
        kind: str,
        lane: str,
        agent_id: str | None,
        session_id: str | None,
        payload: dict[str, Any],
    ) -> Task:
        """在调用方事务内写入 task；事务提交后唤醒 worker。种类必须有处理器（accept）。"""
        if kind not in self._kinds:
            raise RuntimeError(f"no handler is registered for task kind {kind!r}")
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
        self._wake_after_commit(s)
        return task

    async def claim(self, kinds: Collection[str]) -> Task | None:
        """领取一个可以执行的 task，只在调用方能处理的这些种类里找。"""
        return await self._claim(_claimable().where(Task.kind.in_(list(kinds))).order_by(Task.id).limit(1))

    async def claim_task(self, task_id: int, kinds: Collection[str]) -> Task | None:
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

    async def fail(self, task_id: int, error: str, *, retry: RetryPolicy | None, on_final: Settle) -> Task:
        """记下一次失败：按 retry 退避后重试，尝试次数已用完或 retry 为 None（不重试的失败）时记为最终失败。"""
        async with self._db.transaction() as s:
            task = await s.get(Task, task_id)
            if task is None:
                raise RuntimeError(f"task {task_id} vanished while failing it")
            task.error = error
            if retry is not None and task.attempts < retry.max_attempts:
                task.status = TaskStatus.PENDING
                task.not_before = self._clock() + timedelta(seconds=retry.delay(task.attempts))
            else:
                task.status = TaskStatus.FAILED
                task.finished_at = self._clock()
                await on_final(s, task)
        # 重试时间点或同 lane 的后续 task 需要重新计算等待
        self.wake.set()
        return task

    async def finish_pending_in(self, s: AsyncSession, lane: str, kind: str) -> list[Task]:
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

    async def recover(self, kinds: Collection[str]) -> list[Task]:
        """启动阶段（worker 开始领取之前）调用：上次进程遗留的这些种类的 running 复位为 pending，返回被恢复的
        task。按种类恢复，因为用完尝试次数的要由它的处理器收尾；没有处理器的种类不复位（复位了也不会
        被领取），由 check_kinds 报出。尝试次数是否已经用完由调用方判断（Worker.recover 把用完的记为最终失败）。"""
        async with self._db.transaction() as s:
            running = select(Task).where(Task.status == TaskStatus.RUNNING, Task.kind.in_(list(kinds)))
            tasks = list((await s.scalars(running)).all())
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
            if task.kind != EVENT_TASK and await self.lane_busy(s, task.lane):
                raise Conflict(
                    f"lane {task.lane} still has unfinished tasks; retry task {task_id} after they finish"
                )
            task.status = TaskStatus.PENDING
            task.attempts = 0
            task.not_before = self._clock()
            task.finished_at = None
            self._wake_after_commit(s)
            return task

    async def prune_done(self, kinds: Collection[str], *, finished_before: datetime) -> int:
        """删掉这些种类里在 finished_before 之前就已完成（done）的 task，返回删除的条数（保留策略，platform.retention）。"""
        async with self._db.transaction() as s:
            result = await s.execute(
                delete(Task).where(
                    Task.kind.in_(list(kinds)),
                    Task.status == TaskStatus.DONE,
                    Task.finished_at < finished_before,
                )
            )
            return result.rowcount

    async def unfinished_kinds(self) -> set[str]:
        """还没完成（pending、running）的 task 有哪些种类（启动时核对每种都有处理器，见 check_kinds）。"""
        async with self._db.transaction() as s:
            query = (
                select(Task.kind).where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING])).distinct()
            )
            return set((await s.scalars(query)).all())

    async def next_due(self, kinds: Collection[str]) -> datetime | None:
        """最早一个「可领取、只是还没到时间」的 task 的时间；被同 lane 挡住的 task 由 complete/fail 唤醒。"""
        claimable = _claimable().where(Task.kind.in_(list(kinds))).subquery()
        async with self._db.transaction() as s:
            return await s.scalar(select(func.min(claimable.c.not_before)))

    async def list(self, *, status: TaskStatus | None, limit: int) -> list[Task]:
        query = select(Task).order_by(Task.id.desc()).limit(limit)
        if status is not None:
            query = query.where(Task.status == status)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    # ---- 别的领域看自己 lane 上的任务（只读，在调用方的事务里；返回 TaskView） ----

    async def task_in(self, s: AsyncSession, task_id: int) -> TaskView | None:
        task = await s.get(Task, task_id)
        return None if task is None else TaskView.of(task)

    async def latest_in(self, s: AsyncSession, lanes: Collection[str], kind: str) -> dict[str, TaskView]:
        """这些 lane 上各自最新的一个这种 task（一次查询）；没有的 lane 不在结果里。"""
        latest = (
            select(func.max(Task.id)).where(Task.lane.in_(list(lanes)), Task.kind == kind).group_by(Task.lane)
        )
        rows = (await s.scalars(select(Task).where(Task.id.in_(latest)))).all()
        return {t.lane: TaskView.of(t) for t in rows}

    async def has_unfinished_in(
        self,
        s: AsyncSession,
        lane: str,
        kind: str,
        *,
        pending_only: bool = False,
        excluding: int | None = None,
    ) -> bool:
        """这个 lane 上有没有排着或进行中的这种 task；pending_only 只看排着的，excluding 不算这一个（调用方正在收尾
        的那个，队列稍后才把它记为完成）。"""
        statuses = [TaskStatus.PENDING] if pending_only else [TaskStatus.PENDING, TaskStatus.RUNNING]
        query = select(Task.id).where(Task.lane == lane, Task.kind == kind, Task.status.in_(statuses))
        if excluding is not None:
            query = query.where(Task.id != excluding)
        return bool(await s.scalar(select(query.exists())))

    async def update_payload_in(self, s: AsyncSession, task_id: int, payload: Mapping[str, Any]) -> None:
        """在调用方事务里改写一个 task 的 payload（它的处理器记下执行中途的进展，如运行取走的信件）。"""
        task = await s.get(Task, task_id)
        if task is None:
            raise RuntimeError(f"task {task_id} vanished while updating its payload")
        task.payload = dict(payload)

    async def lane_busy(self, s: AsyncSession, lane: str) -> bool:
        return bool(await self.busy_lanes(s, [lane]))

    async def busy_lanes(self, s: AsyncSession, lanes: Collection[str]) -> set[str]:
        """其中有排队或进行中任务的 lane（一次查询）。"""
        busy = await s.scalars(
            select(Task.lane)
            .where(Task.lane.in_(list(lanes)), Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
            .distinct()
        )
        return set(busy.all())
