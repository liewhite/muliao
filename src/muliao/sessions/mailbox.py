"""会话信箱：一切要交给 agent 的东西都是往会话信箱里投一封信件（mail 表），消费者（sessions.consumer）按到达顺序把
信件交给模型。设计见 docs/architecture.md「会话信箱」。

- 投信（post_in）：用户消息、对挂起调用的回答（用户回答提问、被问的 agent 交回结局）、触发器事件、委派来的任务、
  后台任务与 thread 的事件、运行中检查。
- 一封信件在交给模型的那一步保存时记为已处理（consume_in，与那一步的消息同一个事务）。
- 用户停止会话时信箱清空（discard_in）：没交给模型的信件记为被丢弃，留着记录。
- 唤醒（wake_in，唯一规则）：会话里有排队的信件、没有排队或运行中的运行，并且有信件在最近一次运行之后才到；
  会话停在挂起的调用上时按 sessions.pending.waiting 等回答或新消息，事件等它们一起交给模型。调用方：投信时、运行
  结束时（含最终失败，RunHandler.failed_in 结清之后）、定期对账（MailReconciler，兜住投递时事务失败与重启）。
  「最近一次运行之后才到」保证最终失败的运行不会因为同一批信件被无限重新投递：它被重试、或新信件到达时再处理。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import NotFound
from muliao.platform.tasks import TaskQueue
from muliao.sessions.history import since_last_response, since_last_response_among
from muliao.sessions.holds import called_in, get_in
from muliao.sessions.lanes import RUN_TASK, session_lane
from muliao.sessions.pending import OpenAsks, open_asks_of, waiting
from muliao.sessions.runs import RunPayload, handed_to, latest_run, run_unfinished_in
from muliao.sessions.tables import HoldStatus, Mail, MailKind, MailState, Session

log = logging.getLogger(__name__)

_SOURCE_MAX = 100


async def post_in(
    s: AsyncSession,
    queue: TaskQueue,
    session_id: str,
    *,
    kind: MailKind,
    content: str,
    awaited: bool,
    source: str | None = None,
    check_in: bool = False,
    attachments: Sequence[str] = (),
    answers: Mapping[str, Any] | None = None,
    correlation: str | None = None,
    dedupe_key: str | None = None,
) -> Mail:
    """在调用方事务里往会话信箱投一封信件，需要时唤醒消费者。带 dedupe_key 的投信只投一次：已经投过就返回那封。"""
    if kind == MailKind.EVENT and (not source or len(source) > _SOURCE_MAX or '"' in source):
        raise ValueError(f"event source must be 1-{_SOURCE_MAX} characters without quotes, got {source!r}")
    if check_in and kind != MailKind.EVENT:
        raise ValueError(f"only events can be check-ins, got a {kind} mail")
    session = await s.get(Session, session_id)
    if session is None:
        raise NotFound(f"session {session_id!r} not found")
    if dedupe_key is not None and (existing := await _with_key_in(s, session_id, dedupe_key)):
        log.info("duplicate mail ignored", extra={"session_id": session_id, "key": dedupe_key})
        return existing
    mail = Mail(
        session_id=session_id,
        kind=kind,
        source=source,
        content=content,
        check_in=check_in,
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


async def _with_key_in(s: AsyncSession, session_id: str, key: str) -> Mail | None:
    return await s.scalar(select(Mail).where(Mail.session_id == session_id, Mail.dedupe_key == key))


async def posted_with_key_in(s: AsyncSession, session_id: str, key: str) -> bool:
    """带这个幂等键的信件已经投过。"""
    return await _with_key_in(s, session_id, key) is not None


async def queued_in(s: AsyncSession, session_id: str) -> list[Mail]:
    """会话里还在排队的信件，按到达顺序。"""
    rows = await s.scalars(
        select(Mail).where(Mail.session_id == session_id, Mail.state == MailState.QUEUED).order_by(Mail.id)
    )
    return list(rows.all())


async def queued_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, list[Mail]]:
    """这些会话里还在排队的信件，各自按到达顺序（一次查询）。"""
    rows = await s.scalars(
        select(Mail)
        .where(Mail.session_id.in_(list(session_ids)), Mail.state == MailState.QUEUED)
        .order_by(Mail.id)
    )
    queued: dict[str, list[Mail]] = {x: [] for x in session_ids}
    for mail in rows.all():
        queued[mail.session_id].append(mail)
    return queued


async def wake_in(
    s: AsyncSession, queue: TaskQueue, session: Session, *, finished: int | None = None
) -> bool:
    """信箱需要消费时投递一次运行（规则见模块说明）；返回是否投递了。

    finished：刚正常结束的那次运行（在它的收尾事务里调用时）。队列在同一个事务里稍后才把它记为完成，此刻它还是
    running，不能因此判定会话忙；它也不是「失败待重试」，剩下的信件（它没取走的，如从历史续跑、只重发了一条请求
    就结束）一定要有下一次运行，所以不做「之后才到」的判断。"""
    queued = await queued_in(s, session.id)
    if not queued:
        return False
    if await run_unfinished_in(s, queue, session.id, excluding=finished):
        # 排队的运行开始时、运行中的那次每一步都会取走新信件；它结束时会再来这里
        return False
    latest = await latest_run(s, queue, session.id)
    if finished is None and latest is not None and all(handed_to(m.id, latest) for m in queued):
        # 都已经交给过一次运行（它失败了、等重试）：由它或之后新信件唤醒的运行处理，不重复投递
        return False
    if waiting(await since_last_response(s, session.id), queued, await called_in(s, session.id)):
        # 停在挂起的调用上：等回答或新消息（跳过），事件到时一起交给模型。挂起的调用只可能在最后一条模型回复里，
        # 不加载整段上下文
        return False
    queue.enqueue(
        s,
        kind=RUN_TASK,
        lane=session_lane(session.id),
        agent_id=session.agent_id,
        session_id=session.id,
        payload=RunPayload(through_mail_id=queued[-1].id).model_dump(),
    )
    log.info("consumer woken", extra={"session_id": session.id, "queued": len(queued)})
    return True


async def unsettled_in(s: AsyncSession, session_id: str, states: Sequence[MailState]) -> list[Mail]:
    """会话里带关联、还没结清的信件（结清的时机与范围在 sessions.settlement）。"""
    rows = await s.scalars(
        select(Mail)
        .where(
            Mail.session_id == session_id,
            Mail.correlation.is_not(None),
            Mail.correlation_settled.is_(False),
            Mail.state.in_(states),
        )
        .order_by(Mail.id)
    )
    return list(rows.all())


async def with_unsettled_in(s: AsyncSession) -> list[str]:
    """有带关联、还没结清的信件（排着的或已交给模型的）的会话（信箱对账补做失败的结清）。"""
    rows = await s.scalars(
        select(Mail.session_id)
        .where(
            Mail.correlation.is_not(None),
            Mail.correlation_settled.is_(False),
            Mail.state.in_([MailState.CONSUMED, MailState.QUEUED]),
        )
        .distinct()
    )
    return list(rows.all())


def mark_settled(mail: Sequence[Mail]) -> list[str]:
    """把这些信件带来的关联记为已结清（随调用方的事务提交），返回这次新结清的关联（去重、按信件顺序）。"""
    correlations = list(
        dict.fromkeys(m.correlation for m in mail if m.correlation and not m.correlation_settled)
    )
    for m in mail:
        if m.correlation:
            m.correlation_settled = True
    return correlations


async def with_queued_in(s: AsyncSession) -> list[str]:
    """有信件在排队的会话，按 id 排列（信箱对账的唤醒）。"""
    rows = await s.scalars(
        select(Mail.session_id).where(Mail.state == MailState.QUEUED).distinct().order_by(Mail.session_id)
    )
    return list(rows.all())


async def consumed_by_in(s: AsyncSession, session_id: str, run_id: int) -> list[Mail]:
    """这次运行处理掉的信件（带上会话条件：按会话的索引只扫这个会话的行）。"""
    rows = await s.scalars(select(Mail).where(Mail.session_id == session_id, Mail.consumed_by == run_id))
    return list(rows.all())


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


async def post_check_in(
    s: AsyncSession, queue: TaskQueue, session_id: str, holder: str, source: str, text: str
) -> bool:
    """运行中检查（platform.checks.WorkWatcher 的节奏）：在调用方事务里给发起会话投一封检查信件（不推送）；这个来源
    已有信件排着时不投（不堆积）。工作的占用已不是 active（会话被停止，或
    会话已删除）时也不投：停止之后不唤醒会话。返回是否投了。"""
    hold = await get_in(s, session_id, holder)
    if hold is None:
        if await s.get(Session, session_id) is not None:
            raise RuntimeError(f"session {session_id} has no hold {holder} for its running work")
        return False
    if hold.status != HoldStatus.ACTIVE:
        return False
    if await has_queued_from(s, session_id, source):
        return False
    await post_in(
        s,
        queue,
        session_id,
        kind=MailKind.EVENT,
        source=source,
        content=text,
        awaited=False,
        check_in=True,
    )
    return True


async def open_asks_in(s: AsyncSession, session_id: str) -> OpenAsks:
    """会话还没有结果的提问（规则在 pending.open_asks_of）。"""
    return open_asks_of(await since_last_response(s, session_id), await queued_in(s, session_id))


async def open_asks_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, OpenAsks]:
    """open_asks_in 的批量版本（会话列表的工作状态），同一条规则。"""
    tails = await since_last_response_among(s, session_ids)
    queued = await queued_among(s, session_ids)
    return {x: open_asks_of(tails[x], queued[x]) for x in session_ids}
