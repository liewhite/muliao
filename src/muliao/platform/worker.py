import logging
from typing import Protocol

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import TransientError
from muliao.platform.bus import EventBus, TaskEvent
from muliao.platform.tables import EVENT_TASK, Task, TaskStatus
from muliao.platform.tasks import Commit, TaskQueue

log = logging.getLogger(__name__)
# run_now 被取消后把任务放回 pending 的时限（一个短事务）
_UNCLAIM_SECONDS = 10.0

_IDLE_POLL_SECONDS = 30.0


def is_retryable(exc: BaseException) -> bool:
    """按契约重试的失败：领域与适配层把暂时性失败表达为 TransientError（模型与网络层的由 agent_runtime 换成它），
    加上超时。别的异常一律按确定性失败记账：平台不认识任何 SDK 的异常类型。"""
    return isinstance(exc, (TransientError, TimeoutError))


class TaskHandler(Protocol):
    async def execute(self, task: Task) -> Commit: ...

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """task 最终失败（不再重试）时，在记下失败的同一事务里收尾。失败本身已可见（任务记录与日志）。"""
        ...


class Interrupted(Exception):
    """任务被中断（进程退出）后恢复的次数已经用完：不再恢复，记为最终失败。见 Worker._recover。"""


class _SettleFailed(Exception):
    """task 最终失败时的收尾钩子（TaskHandler.failed_in）抛出了异常；与记账本身的失败区分开。"""


async def _unsettled(_s: AsyncSession, _task: Task) -> None:
    """不收尾：收尾钩子本身失败时的退路（见 Worker._record_failure）。"""


class Worker:
    def __init__(
        self,
        queue: TaskQueue,
        handlers: dict[str, TaskHandler],
        bus: EventBus,
        *,
        concurrency: int,
    ):
        self._queue = queue
        self._handlers = handlers
        self._bus = bus
        self._slots = anyio.Semaphore(concurrency)

    @property
    def kinds(self) -> frozenset[str]:
        """这个 worker 处理的任务种类（组合根登记的处理器）。"""
        return frozenset(self._handlers)

    def _publish(self, task: Task, status: TaskStatus, error: str | None = None) -> None:
        self._bus.publish(
            TaskEvent(
                task_id=task.id,
                kind=task.kind,
                status=status,
                session_id=task.session_id,
                agent=task.agent_id,
                error=error,
            )
        )

    async def run_forever(self) -> None:
        await self._recover()
        # 执行中的任务都属于这个任务组：worker 被取消（关停）时它们一起被取消，不会留在后台
        async with anyio.create_task_group() as group:
            while True:
                await self._slots.acquire()
                task = await self._queue.claim(self._handlers.keys())
                if task is None:
                    self._slots.release()
                    await self._wait_for_work()
                    continue
                group.start_soon(self._process_in_slot, task, name=f"task-{task.id}")

    async def _recover(self) -> None:
        """启动时把上次进程遗留的 running 复位为 pending 重新执行（队列的 recover），尝试次数已经用完的除外：
        任务本身会让进程退出时（例如它执行的命令重启了服务），恢复—重启—再恢复会无限循环下去，还一直占住它的 lane。
        这时按最终失败记账（和重试用完一样的上限 task_max_attempts，钩子照常收尾、失败照常可见），用户在「任务」页
        手动重试即可重新开始（重试把尝试次数清零）。不归这个 worker 处理的种类照常复位，留给处理它的 worker。"""
        for task in await self._queue.recover():
            ctx = {"task_id": task.id, "kind": task.kind, "lane": task.lane, "attempt": task.attempts}
            if task.attempts >= self._queue.max_attempts and task.kind in self._handlers:
                log.error("recovered task has used up its attempts; failing it instead", extra=ctx)
                await self._record_failure(
                    task,
                    Interrupted(
                        f"interrupted while running; its {task.attempts} attempts are used up, "
                        f"not recovering it again"
                    ),
                    ctx,
                )
            else:
                log.warning("recovered interrupted task", extra=ctx)

    async def run_now(self, task_id: int) -> None:
        """在调用方的任务里立刻执行这一个投递任务（经同一个执行边界：时限、重试或失败记账），不占 worker 的槽位。
        用于发送方提交了定向命令之后希望它马上生效（例如触发器新建的会话在返回会话 id 之前就已存在）；只用于短小的
        投递（EVENT_TASK），运行这类长任务仍由 worker 按并发上限执行。此刻不可领取就什么都不做，照常由 worker 执行。

        取消语义与 worker 不同：worker 的任务只被关停取消，下次启动由 recover 复位；这里的调用方随时可能被取消（请求
        断开、调用它的运行被停止）。被取消时在屏蔽作用域里把还在 running 的它放回 pending 交给 worker，再照常抛出
        取消——投递的写入与「完成」同一事务，重做是安全的。"""
        if EVENT_TASK not in self._handlers:
            raise RuntimeError("run_now executes deliveries, but this worker has no EVENT handler")
        task = await self._queue.claim_task(task_id, [EVENT_TASK])
        if task is None:
            log.info("task not claimable now, left to the worker", extra={"task_id": task_id})
            return
        try:
            await self._process(task)
        except anyio.get_cancelled_exc_class():
            ctx = {"task_id": task.id, "lane": task.lane}
            with anyio.CancelScope(shield=True), anyio.move_on_after(_UNCLAIM_SECONDS) as limited:
                if await self._queue.unclaim(task.id):
                    self._publish(task, TaskStatus.PENDING)
                    log.warning("task run on the spot was cancelled; handed back to the worker", extra=ctx)
            if limited.cancelled_caught:
                log.error(
                    "could not hand back a cancelled task run on the spot; it stays running and blocks its lane "
                    "until the next start recovers it",
                    extra=ctx,
                )
            raise

    async def _process_in_slot(self, task: Task) -> None:
        try:
            await self._process(task)
        finally:
            self._slots.release()

    async def _wait_for_work(self) -> None:
        self._queue.wake.clear()
        due = await self._queue.next_due(self._handlers.keys())
        timeout = _IDLE_POLL_SECONDS if due is None else max(0.05, (due - self._queue.now()).total_seconds())
        with anyio.move_on_after(min(timeout, _IDLE_POLL_SECONDS)):
            await self._queue.wake.wait()

    async def _process(self, task: Task) -> None:
        ctx = {"task_id": task.id, "kind": task.kind, "lane": task.lane, "attempt": task.attempts}
        log.info("task started", extra=ctx)
        self._publish(task, TaskStatus.RUNNING)
        try:
            # 不设总时限：一次运行该跑多久由 agent 自己判断；兜底与收束见 architecture.md「TaskHandler 接口」
            commit = await self._handlers[task.kind].execute(task)
            await self._queue.complete(task.id, commit)
        except anyio.get_cancelled_exc_class():
            # worker 的任务只被关停取消，下次启动由 recover 复位；run_now 的调用方被取消时由 run_now 当场放回
            log.warning("task cancelled", extra=ctx)
            raise
        except Exception as e:
            # 每个任务的异常在这里记账（重试或失败），不向上传播：一个任务出错不影响别的任务
            await self._record_failure(task, e, ctx)
        else:
            log.info("task done", extra=ctx)
            self._publish(task, TaskStatus.DONE)

    async def _record_failure(self, task: Task, exc: Exception, ctx: dict[str, object]) -> None:
        error = f"{type(exc).__name__}: {exc}"
        settle = self._handlers[task.kind].failed_in

        async def settle_or_mark(s: AsyncSession, failing: Task) -> None:
            try:
                await settle(s, failing)
            except Exception as e:
                raise _SettleFailed from e

        try:
            try:
                failed = await self._queue.fail(
                    task.id, error, retryable=is_retryable(exc), on_final=settle_or_mark
                )
            except _SettleFailed:
                # 只有收尾钩子（如把失败交回等着的会话）自己失败时走这里；记账本身的失败（如数据库暂时不可用）
                # 照常交给外层。事务已整体回滚：task 不能因此卡在 running、占住 lane 直到重启，记下钩子的失败后
                # 不带钩子再记一次——失败照样可见，没交回的结果在这条日志里
                log.exception(
                    "settling the failed task failed; recording the failure without settling it", extra=ctx
                )
                failed = await self._queue.fail(
                    task.id, error, retryable=is_retryable(exc), on_final=_unsettled
                )
        except Exception:
            # 记账本身失败（如 task 已被删除）：留下完整上下文，不能让异常无人接收
            log.exception("could not record task failure: %s", error, extra=ctx)
            self._publish(task, TaskStatus.FAILED, error)
            return
        if failed.status == TaskStatus.FAILED:
            log.error("task failed: %s", error, exc_info=exc, extra=ctx)
        else:
            log.warning(
                "task failed transiently, retry scheduled at %s: %s", failed.not_before, error, extra=ctx
            )
        self._publish(task, failed.status, error)
