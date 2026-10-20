import logging
import traceback
from typing import Protocol

import anthropic
import anyio
import httpx
import httpx2
import openai
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import TransientError
from muliao.platform.bus import EventBus, TaskEvent
from muliao.platform.tables import Task, TaskKind, TaskStatus
from muliao.platform.tasks import Commit, TaskQueue

log = logging.getLogger(__name__)

# 模型提供方契约中声明的暂时性状态码
_TRANSIENT_HTTP = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_IDLE_POLL_SECONDS = 30.0


# 网络层的暂时性失败。pydantic-ai 把它们包装为 ModelAPIError，原始异常在 __cause__ 中
_TRANSPORT_ERRORS = (
    httpx.TransportError,
    httpx2.TransportError,
    anthropic.APIConnectionError,
    openai.APIConnectionError,
    TimeoutError,
)


def is_transient(exc: BaseException) -> bool:
    """契约内的暂时性失败才重试。ModelAPIError 还包括请求超限、响应解码失败等确定性错误，不能整类重试。"""
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in _TRANSIENT_HTTP
    if isinstance(exc, ModelAPIError):
        return isinstance(exc.__cause__, _TRANSPORT_ERRORS)
    return isinstance(exc, (TransientError, *_TRANSPORT_ERRORS))


class TaskHandler(Protocol):
    async def execute(self, task: Task) -> Commit: ...

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """task 最终失败（不再重试）时，在记下失败的同一事务里收尾。失败本身已可见（任务记录与日志）。"""
        ...


class _SettleFailed(Exception):
    """task 最终失败时的收尾钩子（TaskHandler.failed_in）抛出了异常；与记账本身的失败区分开。"""


async def _unsettled(_s: AsyncSession, _task: Task) -> None:
    """不收尾：收尾钩子本身失败时的退路（见 Worker._record_failure）。"""


class Worker:
    def __init__(
        self,
        queue: TaskQueue,
        handlers: dict[TaskKind, TaskHandler],
        bus: EventBus,
        *,
        concurrency: int,
        task_timeout_seconds: float,
    ):
        self._task_timeout = task_timeout_seconds
        self._queue = queue
        self._handlers = handlers
        self._bus = bus
        self._slots = anyio.Semaphore(concurrency)

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
        for task in await self._queue.recover():
            log.warning(
                "recovered interrupted task",
                extra={"task_id": task.id, "kind": task.kind, "lane": task.lane, "attempt": task.attempts},
            )
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

    async def run_now(self, task_id: int) -> None:
        """在调用方的任务里立刻执行这一个 task（经同一个执行边界：时限、重试或失败记账），不占 worker 的槽位。用于
        发送方提交了定向命令之后希望它马上生效（例如触发器新建的会话在返回会话 id 之前就已存在）；此刻不可领取就
        什么都不做，照常由 worker 执行。"""
        task = await self._queue.claim_task(task_id)
        if task is None:
            log.info("task not claimable now, left to the worker", extra={"task_id": task_id})
            return
        if task.kind not in self._handlers:
            raise RuntimeError(f"task {task_id} of kind {task.kind} has no handler in this worker")
        await self._process(task)

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
            # 总时限：卡死的工具或模型连接不能永久占住 lane 和 worker 槽位；超时按暂时性失败重试
            with anyio.fail_after(self._task_timeout):
                commit = await self._handlers[task.kind].execute(task)
            await self._queue.complete(task.id, commit)
        except anyio.get_cancelled_exc_class():
            log.warning("task cancelled by shutdown, will be recovered on next start", extra=ctx)
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
                    task.id, error, retryable=is_transient(exc), on_final=settle_or_mark
                )
            except _SettleFailed:
                # 只有收尾钩子（如把失败交回等着的会话）自己失败时走这里；记账本身的失败（如数据库暂时不可用）
                # 照常交给外层。事务已整体回滚：task 不能因此卡在 running、占住 lane 直到重启，记下钩子的失败后
                # 不带钩子再记一次——失败照样可见，没交回的结果在这条日志里
                log.exception(
                    "settling the failed task failed; recording the failure without settling it", extra=ctx
                )
                failed = await self._queue.fail(
                    task.id, error, retryable=is_transient(exc), on_final=_unsettled
                )
        except Exception:
            # 记账本身失败（如 task 已被删除）：留下完整上下文，不能让异常无人接收
            log.exception("could not record task failure: %s", error, extra=ctx)
            self._publish(task, TaskStatus.FAILED, error)
            return
        if failed.status == TaskStatus.FAILED:
            log.error("task failed: %s\n%s", error, traceback.format_exc(), extra=ctx)
        else:
            log.warning(
                "task failed transiently, retry scheduled at %s: %s", failed.not_before, error, extra=ctx
            )
        self._publish(task, failed.status, error)
