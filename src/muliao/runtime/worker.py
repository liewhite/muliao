import asyncio
import logging
import traceback
from typing import Protocol

import httpx
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from muliao.bus import EventBus
from muliao.db import utcnow
from muliao.errors import TransientError
from muliao.models import Task, TaskKind, TaskStatus
from muliao.runtime.tasks import Commit, TaskQueue

log = logging.getLogger(__name__)

# 模型提供方契约中声明的暂时性状态码
_TRANSIENT_HTTP = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_IDLE_POLL_SECONDS = 30.0


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in _TRANSIENT_HTTP
    if isinstance(exc, ModelAPIError | TransientError | httpx.TransportError | TimeoutError):
        return True
    return False


class TaskHandler(Protocol):
    async def execute(self, task: Task) -> Commit: ...


class Worker:
    def __init__(
        self, queue: TaskQueue, handlers: dict[TaskKind, TaskHandler], bus: EventBus, concurrency: int
    ):
        self._queue = queue
        self._handlers = handlers
        self._bus = bus
        self._slots = asyncio.Semaphore(concurrency)
        self._running: set[asyncio.Task[None]] = set()

    def _publish(self, task: Task, status: TaskStatus, error: str | None = None) -> None:
        self._bus.publish(
            {
                "type": "task",
                "task_id": task.id,
                "kind": task.kind,
                "status": status,
                "session_id": task.session_id,
                "agent": task.agent_name,
                "error": error,
            }
        )

    async def run_forever(self) -> None:
        recovered = await self._queue.recover()
        if recovered:
            log.warning("recovered interrupted tasks", extra={"count": recovered})
        try:
            while True:
                await self._slots.acquire()
                task = await self._queue.claim()
                if task is None:
                    self._slots.release()
                    await self._wait_for_work()
                    continue
                t = asyncio.create_task(self._process(task), name=f"task-{task.id}")
                self._running.add(t)
                t.add_done_callback(self._running.discard)
        finally:
            for t in self._running:
                t.cancel()
            await asyncio.gather(*self._running, return_exceptions=True)

    async def _wait_for_work(self) -> None:
        self._queue.wake.clear()
        due = await self._queue.next_due()
        timeout = _IDLE_POLL_SECONDS if due is None else max(0.05, (due - utcnow()).total_seconds())
        try:
            await asyncio.wait_for(self._queue.wake.wait(), timeout=min(timeout, _IDLE_POLL_SECONDS))
        except TimeoutError:
            pass

    async def _process(self, task: Task) -> None:
        ctx = {"task_id": task.id, "kind": task.kind, "lane": task.lane, "attempt": task.attempts}
        log.info("task started", extra=ctx)
        self._publish(task, TaskStatus.RUNNING)
        try:
            commit = await self._handlers[task.kind].execute(task)
            await self._queue.complete(task.id, commit)
        except asyncio.CancelledError:
            log.warning("task cancelled by shutdown, will be recovered on next start", extra=ctx)
            raise
        except Exception as e:
            retryable = is_transient(e)
            error = f"{type(e).__name__}: {e}"
            failed = await self._queue.fail(task.id, error, retryable=retryable)
            if failed.status == TaskStatus.FAILED:
                log.error("task failed: %s\n%s", error, traceback.format_exc(), extra=ctx)
            else:
                log.warning(
                    "task failed transiently, retry scheduled at %s: %s", failed.not_before, error, extra=ctx
                )
            self._publish(task, failed.status, error)
        else:
            log.info("task done", extra=ctx)
            self._publish(task, TaskStatus.DONE)
        finally:
            self._slots.release()
