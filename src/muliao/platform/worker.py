import logging
from typing import Protocol

import anyio
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.bus import EventBus, TaskEvent
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tasks import Commit, TaskQueue

log = logging.getLogger(__name__)

_IDLE_POLL_SECONDS = 30.0


class TaskHandler(Protocol):
    # 这种任务完成后没有别的数据引用它，过了保留期可以删掉（platform.retention）；由处理它的领域声明
    prunable: bool

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
    def prunable_kinds(self) -> frozenset[str]:
        """完成后可以按保留期删掉的种类（各处理器的 prunable）。"""
        return frozenset(k for k, h in self._handlers.items() if h.prunable)

    @property
    def kinds(self) -> frozenset[str]:
        """这个 worker 处理的任务种类（组合根登记的处理器）。"""
        return frozenset(self._handlers)

    def _publish(self, task: Task, status: TaskStatus) -> None:
        self._bus.publish(TaskEvent(status=status, session_id=task.session_id))

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
        执行（队列的 recover）；放在领取循环里时，抢先开始的 task 会被复位、执行两次。
        尝试次数已经用完的除外：
        任务本身会让进程退出时（例如它执行的命令重启了服务），恢复—重启—再恢复会无限循环下去，还一直占住它的 lane。
        这时按最终失败记账（上限取各重试策略中最大的 Retries.max_attempts，钩子照常收尾、失败照常可见），用户在「任务」页
        手动重试即可重新开始（重试把尝试次数清零）。只恢复自己的种类（TaskQueue.recover）。"""
        for task in await self._queue.recover(self.kinds):
            ctx = {"task_id": task.id, "kind": task.kind, "lane": task.lane, "attempt": task.attempts}
            if task.attempts >= self._queue.retries.max_attempts:
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
            # worker 的任务只被关停取消，下次启动由 recover 复位
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
        retry = self._queue.retries.for_failure(exc)

        async def settle_or_mark(s: AsyncSession, failing: Task) -> None:
            try:
                await settle(s, failing)
            except Exception as e:
                raise _SettleFailed from e

        try:
            try:
                failed = await self._queue.fail(task.id, error, retry=retry, on_final=settle_or_mark)
            except _SettleFailed:
                # 只有收尾钩子（如把失败交回等着的会话）自己失败时走这里；记账本身的失败（如数据库暂时不可用）
                # 照常交给外层。事务已整体回滚：task 不能因此卡在 running、占住 lane 直到重启，记下钩子的失败后
                # 不带钩子再记一次——失败照样可见，没交回的结果在这条日志里
                log.exception(
                    "settling the failed task failed; recording the failure without settling it", extra=ctx
                )
                failed = await self._queue.fail(task.id, error, retry=retry, on_final=_unsettled)
        except Exception:
            # 记账本身失败（如 task 已被删除）：留下完整上下文，不能让异常无人接收
            log.exception("could not record task failure: %s", error, extra=ctx)
            self._publish(task, TaskStatus.FAILED)
            return
        if failed.status == TaskStatus.FAILED:
            log.error("task failed: %s", error, exc_info=exc, extra=ctx)
        else:
            log.warning(
                "task failed transiently, retry scheduled at %s: %s", failed.not_before, error, extra=ctx
            )
        self._publish(task, failed.status)
