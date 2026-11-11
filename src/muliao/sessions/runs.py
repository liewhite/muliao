"""会话的运行（run task，队列里种类为 RUN_TASK 的任务）：查最近一次运行、失败的说明。运行在平台的任务表里（lane 为
会话的 lane）；会话领域经 TaskQueue 的只读查询（TaskView）看它们，不查任务表。运行的 payload 为空：交给它的信件记在
信上（mail.taken_by，sessions.mailbox）。"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.tasks import TaskQueue, TaskView
from muliao.sessions.lanes import RUN_TASK, session_lane


async def latest_run(s: AsyncSession, queue: TaskQueue, session_id: str) -> TaskView | None:
    """会话最近一次运行。"""
    return (await latest_runs_among(s, queue, [session_id])).get(session_id)


async def latest_runs_among(
    s: AsyncSession, queue: TaskQueue, session_ids: Collection[str]
) -> dict[str, TaskView]:
    """这些会话各自最近一次运行（一次查询）；还没有运行的会话不在结果里。"""
    lanes = {session_lane(x): x for x in session_ids}
    return {lanes[lane]: run for lane, run in (await queue.latest_in(s, list(lanes), RUN_TASK)).items()}


async def runs_between_in(
    s: AsyncSession, queue: TaskQueue, session_ids: Collection[str], start: datetime, end: datetime
) -> dict[str, list[TaskView]]:
    """这些会话在 [start, end) 里排上的运行，各自按先后（一次查询）；没有的会话不在结果里。"""
    lanes = {session_lane(x): x for x in session_ids}
    runs = await queue.created_between_in(s, list(lanes), RUN_TASK, start, end)
    return {lanes[lane]: found for lane, found in runs.items()}


async def run_unfinished_in(s: AsyncSession, queue: TaskQueue, session_id: str) -> bool:
    """会话有排着或进行中的运行（在运行的收尾里调用时，它自己已经记为完成，TaskQueue.complete）。"""
    return await queue.has_unfinished_in(s, session_lane(session_id), RUN_TASK)


def failure_of(run: TaskView) -> str:
    """失败的运行的错误说明；队列契约保证 failed 一定带 error。"""
    if not run.failed or run.error is None:
        raise RuntimeError(
            f"run {run.id} is {run.status} with error {run.error!r}, expected a recorded failure"
        )
    return run.error


def recorded_failure(run: TaskView | None) -> str | None:
    """最近一次运行最终失败时的错误说明（之后又有运行排上就不再算）；没有运行或不是失败为 None。"""
    return failure_of(run) if run is not None and run.failed else None
