"""事件来源：事件信件的 source（交给模型时写在 `<event source="...">` 里）。格式只在这里写一次：thread 交回的是
`thread:<子会话 id>`，后台任务的是 `job:<任务号>`（也是它在会话上的占用 holder），别的来源原样。界面拿到的是解析好的
EventSource，不自己解析字符串。"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field

_THREAD = re.compile(r"\Athread:(?P<id>.+)\Z")
_JOB = re.compile(r"\Ajob:(?P<id>\d+)\Z")


def thread_source(child_id: str) -> str:
    """thread 交回的事件的来源，也用来判断它是否已有信件在发起方的信箱里排着。"""
    return f"thread:{child_id}"


def job_source(job_id: int) -> str:
    """后台任务在会话上的占用者（sessions.holds）：结束与运行中检查的命令按它找到占用，事件的来源与幂等键由会话领域
    按它推出。"""
    return f"job:{job_id}"


def job_id_of(holder: str) -> int | None:
    """占用者是后台任务时的任务号（job_source 的逆运算）；别的占用者为 None。"""
    m = _JOB.match(holder)
    return None if m is None else int(m["id"])


class ThreadSource(BaseModel):
    kind: Literal["thread"]
    # 原文，同一来源的事件按它归在一起
    raw: str
    # 交回的 thread（子会话）
    session_id: str


class JobSource(BaseModel):
    kind: Literal["job"]
    raw: str
    job_id: int


class OtherSource(BaseModel):
    kind: Literal["other"]
    raw: str


EventSource = Annotated[ThreadSource | JobSource | OtherSource, Field(discriminator="kind")]


def parse_source(raw: str) -> ThreadSource | JobSource | OtherSource:
    if (m := _THREAD.match(raw)) is not None:
        return ThreadSource(kind="thread", raw=raw, session_id=m["id"])
    if (job_id := job_id_of(raw)) is not None:
        return JobSource(kind="job", raw=raw, job_id=job_id)
    return OtherSource(kind="other", raw=raw)
