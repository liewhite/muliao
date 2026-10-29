"""等待项：会话为什么在 waiting（会话页列出，#298）。来源只有工作状态的唯一规则 activity.Waiting——active 占用与还能
回答的提问；这里只把不透明的占用者认成具体的工作并补上它的参数：委派、到点唤醒在会话的子域里查，后台任务只给任务号
（命令、日志属于工作区领域，由组合层去取）。认不出的占用者是缺陷，直接报错。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.activity import Waiting
from muliao.sessions.delegations import records_among as delegations_among
from muliao.sessions.sources import job_id_of_holder
from muliao.sessions.wakeups import records_among as wakeups_among


class JobWait(BaseModel):
    """在等后台任务（run_shell background）结束。"""

    kind: Literal["job"] = "job"
    job_id: int
    since: datetime


class DelegationWait(BaseModel):
    """在等委派出去的工作交回结局（ask_agent，或 delegate_task 带 report_back）。"""

    kind: Literal["delegation"] = "delegation"
    # 做它的 thread（会话详情的 threads 里有它的 agent 与名字）；thread 已删除为 null
    thread_session_id: str | None
    # report_back 的心跳间隔（秒）；ask_agent 为 null
    check_seconds: float | None
    since: datetime


class WakeWait(BaseModel):
    """在等自己约的到点唤醒（wake_me）。"""

    kind: Literal["wake"] = "wake"
    due_at: datetime
    note: str
    since: datetime


class AskWait(BaseModel):
    """在等用户回答提问（ask_user）。"""

    kind: Literal["ask"] = "ask"
    # 提问的工具调用 id（历史条目里同一个调用）
    call_id: str
    # 各问题的短标题
    headers: list[str]


Wait = Annotated[JobWait | DelegationWait | WakeWait | AskWait, Field(discriminator="kind")]


async def waits_of_in(s: AsyncSession, waiting: Waiting) -> list[Wait]:
    """会话在等的东西逐项展开：占用按登记先后，提问在最后。"""
    holders = [h.holder for h in waiting.holds]
    delegations = await delegations_among(s, holders)
    wakeups = await wakeups_among(s, holders)
    waits: list[Wait] = []
    for hold in waiting.holds:
        job_id = job_id_of_holder(hold.holder)
        if job_id is not None:
            waits.append(JobWait(job_id=job_id, since=hold.created_at))
        elif (d := delegations.get(hold.holder)) is not None:
            waits.append(
                DelegationWait(
                    thread_session_id=d.thread_id,
                    check_seconds=d.check_seconds,
                    since=hold.created_at,
                )
            )
        elif (w := wakeups.get(hold.holder)) is not None:
            waits.append(WakeWait(due_at=w.due_at, note=w.note, since=hold.created_at))
        else:
            raise RuntimeError(
                f"active hold {hold.holder!r} of session {hold.session_id} is of no known kind"
            )
    waits.extend(
        AskWait(call_id=call_id, headers=[q.header for q in questions])
        for call_id, questions in waiting.asks.items()
    )
    return waits
