"""会话信箱：一切要交给 agent 的东西都是往会话信箱里投一封信件（mail 表），消费者（runtime.consumer）按到达顺序把
信件交给模型。设计见 docs/architecture.md「会话信箱」。

- 投信（post_in）：用户消息、对挂起调用的回答（用户回答提问、被问的 agent 交回结局）、触发器事件、委派来的任务、
  后台任务与 thread 的事件、运行中检查。
- 一封信件在交给模型的那一步保存时记为已处理（consume_in，与那一步的消息同一个事务）。
- 用户停止会话时信箱清空（discard_in）：没交给模型的信件记为被丢弃，留着记录。
- 唤醒（wake_in，唯一规则）：会话里有排队的信件、没有排队或运行中的运行，并且有信件在最近一次运行之后才到；
  会话停在挂起的调用上时按 runtime.pending.waiting 等回答或新消息，事件等它们一起交给模型。调用方：投信时、运行
  结束时、定期对账（MailReconciler，兜住投递时事务失败与重启）。
  「最近一次运行之后才到」保证最终失败的运行不会因为同一批信件被无限重新投递：它被重试、或新信件到达时再处理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.db import Database
from muliao.errors import CorruptState, NotFound
from muliao.models import Mail, MailKind, MailState, Session, Task, TaskKind, TaskStatus
from muliao.runtime.history import load_context
from muliao.runtime.holds import called_in
from muliao.runtime.pending import waiting
from muliao.runtime.tasks import TaskQueue, session_lane

log = logging.getLogger(__name__)

_SOURCE_MAX = 100
_RECONCILE_SECONDS = 30.0


async def post_in(
    s: AsyncSession,
    queue: TaskQueue,
    session_id: str,
    *,
    kind: MailKind,
    content: str,
    awaited: bool,
    source: str | None = None,
    attachments: Sequence[str] = (),
    answers: Mapping[str, Any] | None = None,
    correlation: str | None = None,
    dedupe_key: str | None = None,
) -> Mail:
    """在调用方事务里往会话信箱投一封信件，需要时唤醒消费者。带 dedupe_key 的投信只投一次：已经投过就返回那封。"""
    if kind == MailKind.EVENT and (not source or len(source) > _SOURCE_MAX or '"' in source):
        raise ValueError(f"event source must be 1-{_SOURCE_MAX} characters without quotes, got {source!r}")
    session = await s.get(Session, session_id)
    if session is None:
        raise NotFound(f"session {session_id!r} not found")
    if dedupe_key is not None and (
        existing := await s.scalar(
            select(Mail).where(Mail.session_id == session_id, Mail.dedupe_key == dedupe_key)
        )
    ):
        log.info("duplicate mail ignored", extra={"session_id": session_id, "key": dedupe_key})
        return existing
    mail = Mail(
        session_id=session_id,
        kind=kind,
        source=source,
        content=content,
        attachments=list(attachments),
        answers=dict(answers or {}),
        awaited=awaited,
        correlation=correlation,
        dedupe_key=dedupe_key,
    )
    s.add(mail)
    await s.flush()
    log.info(
        "mail posted", extra={"session_id": session_id, "mail_id": mail.id, "kind": kind, "source": source}
    )
    await wake_in(s, queue, session)
    return mail


async def queued_in(s: AsyncSession, session_id: str) -> list[Mail]:
    """会话里还在排队的信件，按到达顺序。"""
    rows = await s.scalars(
        select(Mail).where(Mail.session_id == session_id, Mail.state == MailState.QUEUED).order_by(Mail.id)
    )
    return list(rows.all())


async def wake_in(
    s: AsyncSession, queue: TaskQueue, session: Session, *, finished: int | None = None
) -> Task | None:
    """信箱需要消费时投递一次运行（规则见模块说明）；返回投递的运行，不需要时为 None。

    finished：刚正常结束的那次运行（在它的收尾事务里调用时）。队列在同一个事务里稍后才把它记为完成，此刻它还是
    running，不能因此判定会话忙；它也不是「失败待重试」，剩下的信件（它没取走的，如从历史续跑、只重发了一条请求
    就结束）一定要有下一次运行，所以不做「之后才到」的判断。"""
    queued = await queued_in(s, session.id)
    if not queued:
        return None
    lane = session_lane(session.id)
    runs = select(Task).where(Task.lane == lane, Task.kind == TaskKind.RUN)
    busy = runs.where(Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]))
    if finished is not None:
        busy = busy.where(Task.id != finished)
    if await s.scalar(select(busy.exists())):
        # 排队的运行开始时、运行中的那次每一步都会取走新信件；它结束时会再来这里
        return None
    latest = await latest_run(s, session.id)
    if finished is None and latest is not None and all(handed_to(m, latest) for m in queued):
        # 都已经交给过一次运行（它失败了、等重试）：由它或之后新信件唤醒的运行处理，不重复投递
        return None
    if waiting((await load_context(s, session.id)).messages, queued, await called_in(s, session.id)):
        # 停在挂起的调用上：等回答或新消息（跳过），事件到时一起交给模型
        return None
    run = queue.enqueue(
        s,
        kind=TaskKind.RUN,
        lane=lane,
        agent_id=session.agent_id,
        session_id=session.id,
        payload=RunPayload(through_mail_id=queued[-1].id).model_dump(),
    )
    log.info("consumer woken", extra={"session_id": session.id, "queued": len(queued)})
    return run


async def visible_in(s: AsyncSession, session_id: str) -> list[Mail]:
    """界面上显示在历史之后的信件：排队中的，以及最近一次停止丢弃的（之后再有信件交给模型就不再显示，
    那时它们已被后来的对话越过），按到达顺序。"""
    last_consumed = await s.scalar(
        select(func.max(Mail.id)).where(Mail.session_id == session_id, Mail.state == MailState.CONSUMED)
    )
    rows = await s.scalars(
        select(Mail)
        .where(
            Mail.session_id == session_id,
            or_(
                Mail.state == MailState.QUEUED,
                and_(Mail.state == MailState.DISCARDED, Mail.id > (last_consumed or 0)),
            ),
        )
        .order_by(Mail.id)
    )
    return list(rows.all())


async def latest_run(s: AsyncSession, session_id: str) -> Task | None:
    """会话最近一次运行。"""
    return await s.scalar(
        select(Task)
        .where(Task.lane == session_lane(session_id), Task.kind == TaskKind.RUN)
        .order_by(Task.id.desc())
        .limit(1)
    )


class RunPayload(BaseModel):
    """一次运行（run task）的 payload，唯一读写处在这里。输入都在信箱里；这里只记投递它时信箱里最新的信件 id。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    through_mail_id: int

    @classmethod
    def of(cls, run: Task) -> RunPayload:
        if run.kind != TaskKind.RUN:
            raise RuntimeError(f"task {run.id} is a {run.kind}, not a run")
        try:
            return cls.model_validate(run.payload)
        except ValidationError as e:
            raise CorruptState(f"run {run.id} has a malformed payload {run.payload!r}: {e}") from e


def handed_to(mail: Mail, run: Task) -> bool:
    """这封排队的信件已经交给过这次运行：投递它时就在信箱里了（它每一次尝试开头都会取走当时排着的信件）。唤醒
    （wake_in 的「之后才到」）与最终失败时了结委派（RunHandler.failed_in）共用这一条规则，按信件 id 比较。"""
    return mail.id <= RunPayload.of(run).through_mail_id


async def consume_in(s: AsyncSession, ids: Sequence[int], run_id: int) -> None:
    """交给模型的那一步保存时记下处理掉的信件。同一会话只有一个消费者，取走的信件不可能被别人处理掉。"""
    if not ids:
        return
    result = await s.execute(
        update(Mail)
        .where(Mail.id.in_(list(ids)), Mail.state == MailState.QUEUED)
        .values(state=MailState.CONSUMED, consumed_by=run_id)
    )
    if result.rowcount != len(ids):  # type: ignore[attr-defined]
        raise RuntimeError(
            f"run {run_id} handed mail {list(ids)} to the model but only {result.rowcount} were still queued"  # type: ignore[attr-defined]
        )


async def discard_in(s: AsyncSession, session_id: str, mail: Sequence[Mail] | None = None) -> list[int]:
    """还没交给模型的信件记为被丢弃，不再处理：用户停止会话时清空整个信箱；给出 mail 时只丢弃这些（最终失败时已经
    交回了失败的委派）。返回丢弃的信件 id。"""
    ids = [m.id for m in (await queued_in(s, session_id) if mail is None else mail)]
    if ids:
        await s.execute(update(Mail).where(Mail.id.in_(ids)).values(state=MailState.DISCARDED))
        log.info("mailbox cleared", extra={"session_id": session_id, "discarded": ids})
    return ids


async def has_queued_from(s: AsyncSession, session_id: str, source: str) -> bool:
    """这个发送方是否已有信件在排队（运行中检查据此不堆积）。"""
    return bool(
        await s.scalar(
            select(
                select(Mail.id)
                .where(Mail.session_id == session_id, Mail.source == source, Mail.state == MailState.QUEUED)
                .exists()
            )
        )
    )


class MailReconciler:
    """定期对账：有信件在排队、却没有被及时唤醒的会话（投递时的事务失败、进程重启），按同一条规则补上。"""

    def __init__(self, db: Database, queue: TaskQueue, interval_seconds: float = _RECONCILE_SECONDS):
        self._db = db
        self._queue = queue
        self._interval = interval_seconds

    async def reconcile(self) -> int:
        """补投递的运行数。"""
        async with self._db.transaction() as s:
            waiting = (
                await s.scalars(
                    select(Session)
                    .where(Session.id.in_(select(Mail.session_id).where(Mail.state == MailState.QUEUED)))
                    .order_by(Session.id)
                )
            ).all()
            woken = [session.id for session in waiting if await wake_in(s, self._queue, session)]
        if woken:
            # 正常路径也会走到这里（最终失败的运行期间到达的信件），所以是 info；补了什么仍然可查
            log.info("reconcile woke consumers for queued mail", extra={"session_ids": woken})
        return len(woken)

    async def run_forever(self) -> None:
        while True:
            await self.reconcile()
            await asyncio.sleep(self._interval)
