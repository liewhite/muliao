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


def register_handlers(*entries: tuple[str, TaskHandler]) -> dict[str, TaskHandler]:
    """组合根登记各任务种类的处理器。同一种类登记两次是写错了，当场报错（dict 字面量会悄悄留下后一个）。"""
    handlers: dict[str, TaskHandler] = {}
    for kind, handler in entries:
        if kind in handlers:
            raise ValueError(f"task kind {kind!r} is registered twice")
        handlers[kind] = handler
    return handlers


class Interrupted(Exception):
    """任务被中断（进程退出）后恢复的次数已经用完：不再恢复，记为最终失败。见 Worker.recover。"""


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
    ):
        self._queue = queue
        self._handlers = handlers
        self._bus = bus

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
        """领取并执行所有可领取的 task，不设并发上限：同一 lane 内的顺序与互斥由队列保证（_claimable），task 本身
        只是一个协程，不是稀缺资源。稀缺的是它们用到的东西，在用的地方限流（模型请求按 provider，见 agent_runtime.llm）。
        曾经按 task 数设槽位：几个长时间的会话运行占满槽位，毫秒级的投递排队 17 分钟，在跑工具、不用模型的运行也
        挡着别的会话调用模型。"""
        # 执行中的任务都属于这个任务组：worker 被取消（关停）时它们一起被取消，不会留在后台
        async with anyio.create_task_group() as group:
            while True:
                task = await self._queue.claim(self.kinds)
                if task is None:
                    await self._wait_for_work()
                    continue
                group.start_soon(self._process, task, name=f"task-{task.id}")

    async def recover(self) -> None:
        """启动阶段（领取循环、HTTP 与别的循环开始之前，由组合根调用）把上次进程遗留的 running 复位为 pending 重新
        执行（队列的 recover）；放在领取循环里时，抢先开始的请求或运行经 run_now 领走的 task 会被复位、执行两次。
        尝试次数已经用完的除外：
        任务本身会让进程退出时（例如它执行的命令重启了服务），恢复—重启—再恢复会无限循环下去，还一直占住它的 lane。
        这时按最终失败记账（和重试用完一样的上限 task_max_attempts，钩子照常收尾、失败照常可见），用户在「任务」页
        手动重试即可重新开始（重试把尝试次数清零）。只恢复自己的种类（TaskQueue.recover）。"""
        for task in await self._queue.recover(self.kinds):
            ctx = {"task_id": task.id, "kind": task.kind, "lane": task.lane, "attempt": task.attempts}
            if task.attempts >= self._queue.max_attempts:
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
        """在调用方的任务里立刻执行这一个投递任务（经同一个执行边界：重试或失败记账）。
        用于发送方提交了定向命令之后希望它马上生效（例如触发器新建的会话在返回会话 id 之前就已存在）；只用于短小的
        投递（EVENT_TASK）。此刻不可领取就什么都不做，照常由 worker 执行。

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

    async def _wait_for_work(self) -> None:
        self._queue.wake.clear()
        due = await self._queue.next_due(self.kinds)
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


class RunNow:
    """「马上执行这个已提交的投递任务」的端口（Worker.run_now），交给提交投递的领域。

    worker 的处理器由各领域的对象组成，这些领域又要马上执行自己提交的投递：端口先构造、交给它们，组合根构造出 worker
    之后 bind。绑定只在装配时发生一次；装配完成前（没有 bind）调用是组合根的 bug，当场报错。"""

    def __init__(self) -> None:
        self._worker: Worker | None = None

    def bind(self, worker: Worker) -> None:
        if self._worker is not None:
            raise RuntimeError("RunNow is already bound to a worker")
        self._worker = worker

    async def __call__(self, task_id: int) -> None:
        if self._worker is None:
            raise RuntimeError(f"RunNow called for task {task_id} before a worker was bound")
        await self._worker.run_now(task_id)
