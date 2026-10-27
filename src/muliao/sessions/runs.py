"""会话的运行（run task，队列里种类为 RUN_TASK 的任务）：查最近一次运行、它的 payload（交给过它的信件）、失败的说明。
运行在平台的任务表里（lane 为会话的 lane）；会话领域经 TaskQueue 的只读查询（TaskView）看它们，不查任务表，读写运行
payload 的规则只在这里。"""

from __future__ import annotations

from collections.abc import Collection

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import CorruptState
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


async def run_unfinished_in(
    s: AsyncSession, queue: TaskQueue, session_id: str, *, excluding: int | None
) -> bool:
    """会话有排着或进行中的运行（excluding：正在收尾的那一次不算，见 mailbox.wake_in）。"""
    return await queue.has_unfinished_in(s, session_lane(session_id), RUN_TASK, excluding=excluding)


class RunPayload(BaseModel):
    """一次运行（run task）的 payload，唯一读写处在这里。输入都在信箱里；这里只记交给过它的最新信件 id：投递它时信箱里
    最新的，运行开头取走了更新的信件时推进到那里（hand_over_in）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    through_mail_id: int

    @classmethod
    def of(cls, run: TaskView) -> RunPayload:
        if run.kind != RUN_TASK:
            raise RuntimeError(f"task {run.id} is a {run.kind}, not a run")
        try:
            return cls.model_validate(run.payload)
        except ValidationError as e:
            raise CorruptState(f"run {run.id} has a malformed payload {run.payload!r}: {e}") from e


def handed_to(mail_id: int, run: TaskView) -> bool:
    """这封排队的信件已经交给过这次运行：投递它时就在信箱里了，或它某次尝试开头取走了它（hand_over_in）。唤醒
    （wake_in 的「之后才到」）与最终失败时了结委派（RunHandler.failed_in）共用这一条规则，按信件 id 比较。"""
    return mail_id <= RunPayload.of(run).through_mail_id


async def hand_over_in(s: AsyncSession, queue: TaskQueue, run_id: int, through_mail_id: int) -> None:
    """运行开头取走了截至这封的排队信件：记进它的 payload（只前进），它们从此算交给过这次运行（handed_to）。"""
    run = await queue.task_in(s, run_id)
    if run is None:
        raise RuntimeError(f"run {run_id} vanished while it was taking mail")
    if through_mail_id > RunPayload.of(run).through_mail_id:
        await queue.update_payload_in(s, run_id, RunPayload(through_mail_id=through_mail_id).model_dump())


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
