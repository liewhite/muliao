"""会话信箱：一切要交给 agent 的东西都是往会话信箱里投一封信件（mail 表），消费者（sessions.consumer）按到达顺序把
信件交给模型。设计见 docs/architecture.md「会话信箱」。

- 投信（post_in）：用户消息、对挂起调用的回答（用户回答提问、被问的 agent 交回结局）、触发器事件、委派来的任务、
  后台任务与 thread 的事件、运行中检查。
- 一封信件交给了哪次运行记在信上（mail.taken_by，写处只有 give_in 与 consume_in）：投递运行时信箱里
  已有的（wake_in）、运行开头取走的（sessions.consumer）、交给模型的那一步保存时——这时同时记为已处理（consume_in，与
  那一步的消息同一个事务）。运行、结清只查信。
- 用户停止会话时信箱清空（discard_in）：没交给模型的信件记为被丢弃，留着记录；对仍挂着的提问的回答在此之前
  已由停止写进历史、记为已处理（sessions.halt.close_history_in）。
- 唤醒（wake_in，唯一规则）：会话里有排队的信件、没有排队或运行中的运行，并且有信件在最近一次运行之后才到；
  会话停在挂起的调用上、还不能继续时（sessions.pending.position 的 Paused.ready）等回答或新消息，事件等它们一起
  交给模型。调用方：投信时、运行结束时（含最终失败，RunHandler.failed_in 结清之后），都与投信或运行的结局同一个事务，
  没有补漏的对账。「最近一次运行之后才到」就是还没交给过它（taken_by），保证最终失败的运行不会因为同一批信件被无限重新投递：它被重试、或新信件
  到达时再处理。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import NotFound
from muliao.platform.tasks import TaskQueue
from muliao.platform.tooling import Audience
from muliao.sessions.history import since_last_response, since_last_response_among
from muliao.sessions.lanes import RUN_TASK, session_lane
from muliao.sessions.origins import Origin
from muliao.sessions.pending import (
    Interrupted,
    OpenAsks,
    Paused,
    Position,
    handed_out_among,
    handed_out_in,
    open_asks_of,
    position,
)
from muliao.sessions.runs import latest_run, run_unfinished_in
from muliao.sessions.tables import Mail, MailKind, MailState, Session

log = logging.getLogger(__name__)


def audience_of(mail: Sequence[Mail], delegated: bool) -> Audience:
    """一次运行的回答交给谁（唯一规则）：它处理的信件有用户在等的就是用户；否则会话有委派来、还没结清的工作
    （delegated：ask_agent 或 report_back，见 sessions.settlement.delegated_in）就是发起委派的 agent——会话做完时
    最终回答在结清里交给它们，不论这次运行处理的是委派那封信，还是之后的后台任务事件、回答、运行中检查；都没有就是
    没人在等。发起方已被停止（占用 cleared）的委派不算：没人再等 thread 的回答（停止已给 thread 排了停止任务）。"""
    if any(m.awaited for m in mail):
        return Audience.USER
    if delegated:
        return Audience.AGENT
    return Audience.NONE


_SOURCE_MAX = 100


async def post_in(
    s: AsyncSession,
    queue: TaskQueue,
    session_id: str,
    *,
    kind: MailKind,
    content: str,
    awaited: bool,
    origin: Origin | None = None,
    source: str | None = None,
    check_in: bool = False,
    attachments: Sequence[str] = (),
    answers: Mapping[str, Any] | None = None,
    delegation: int | None = None,
) -> Mail:
    """在调用方事务里往会话信箱投一封信件，需要时唤醒消费者。
    origin：来信入口投的信件的发送方来源（消息必有，事件没有，回答是 user 或没有）。"""
    if (
        (kind == MailKind.MESSAGE and origin is None)
        or (kind == MailKind.EVENT and origin is not None)
        or (kind == MailKind.ANSWER and origin not in (Origin.USER, None))
    ):
        # 来信入口的回答只有用户对提问的回答；ask_agent 交回的结局（sessions.work）没有来源
        raise ValueError(
            "a message needs its sender's origin, an event has none and an answer is the user's or has none; "
            f"got {kind} from {origin}"
        )
    if kind == MailKind.EVENT and (not source or len(source) > _SOURCE_MAX or '"' in source):
        raise ValueError(f"event source must be 1-{_SOURCE_MAX} characters without quotes, got {source!r}")
    if check_in and kind != MailKind.EVENT:
        raise ValueError(f"only events can be check-ins, got a {kind} mail")
    session = await s.get(Session, session_id)
    if session is None:
        raise NotFound(f"session {session_id!r} not found")
    mail = Mail(
        session_id=session_id,
        kind=kind,
        origin=origin,
        source=source,
        content=content,
        check_in=check_in,
        attachments=list(attachments),
        answers=dict(answers or {}),
        awaited=awaited,
        delegation_id=delegation,
    )
    s.add(mail)
    await s.flush()
    log.info(
        "mail posted", extra={"session_id": session_id, "mail_id": mail.id, "kind": kind, "source": source}
    )
    await wake_in(s, queue, session)
    return mail


async def sent_by_in(
    s: AsyncSession,
    session_ids: Collection[str],
    origin: Origin,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[Mail]:
    """这些会话里这种来源的发送方投的信件（Mail.origin），按到达顺序；给了 start / end 只取 [start, end) 里到的。
    热历史（sessions.recall）按它认「用户说了什么」。"""
    query = select(Mail).where(Mail.session_id.in_(list(session_ids)), Mail.origin == origin)
    if start is not None:
        query = query.where(Mail.created_at >= start)
    if end is not None:
        query = query.where(Mail.created_at < end)
    return list((await s.scalars(query.order_by(Mail.id))).all())


async def first_arrivals_between_in(s: AsyncSession, start: datetime, end: datetime) -> dict[str, datetime]:
    """在 [start, end) 里收到过信件的会话（全部 agent）-> 这段时间里第一封到达的时刻（一次查询）。"""
    rows = await s.execute(
        select(Mail.session_id, func.min(Mail.created_at))
        .where(Mail.created_at >= start, Mail.created_at < end)
        .group_by(Mail.session_id)
    )
    return {x: at for x, at in rows.all()}


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


async def position_in(s: AsyncSession, session_id: str) -> Position:
    """会话现在停在哪（规则在 pending.position）。没出结果的调用只可能在最后一条模型回复里，不加载整段上下文。"""
    return position(
        await since_last_response(s, session_id),
        await handed_out_in(s, session_id),
        await queued_in(s, session_id),
    )


async def wake_in(s: AsyncSession, queue: TaskQueue, session: Session) -> bool:
    """信箱需要消费时投递一次运行（规则见模块说明）；返回是否投递了。在运行的收尾里调用时，它已经记为完成
    （TaskQueue.complete），不算忙。"""
    queued = await queued_in(s, session.id)
    if not queued:
        return False
    if await run_unfinished_in(s, queue, session.id):
        # 排队的运行开始时、运行中的那次每一步都会取走新信件；它结束时会再来这里
        return False
    latest = await latest_run(s, queue, session.id)
    if latest is not None and latest.failed and all(m.taken_by == latest.id for m in queued):
        # 都已经交给过一次最终失败的运行：由它被重试、或之后新信件唤醒的运行处理，不重复投递。正常结束的运行没取走的
        # 信件（如从历史续跑、只重发了一条请求就结束）一定要有下一次运行
        return False
    match position(await since_last_response(s, session.id), await handed_out_in(s, session.id), queued):
        case Paused(ready=False) | Interrupted(after=Paused(ready=False)):
            # 停在挂起的调用上：等回答或新消息（跳过），事件到时一起交给模型
            return False
    run = queue.enqueue(
        s,
        kind=RUN_TASK,
        lane=session_lane(session.id),
        agent_id=session.agent_id,
        session_id=session.id,
        payload={},
    )
    await s.flush()
    # 信箱里已有的信交给这次运行，哪怕它在取走它们之前就最终失败（如开头压缩失败）：不会因为它们再唤醒，失败的结清
    # 也据此了结它们带来的委派（sessions.settlement.settle_failure_in）
    await give_in(s, [m.id for m in queued], run.id)
    log.info("consumer woken", extra={"session_id": session.id, "run_id": run.id, "queued": len(queued)})
    return True


async def give_in(s: AsyncSession, ids: Sequence[int], run_id: int) -> None:
    """这些排着的信件交给这次运行（mail.taken_by 的写处之一，另一处是 consume_in）：投递它时信箱里已有的
    （wake_in）、它开头取走的（sessions.consumer）。交给模型那一步保存时由 consume_in 同时记下。已经不在排队的信件不动。"""
    if ids:
        await s.execute(
            update(Mail).where(Mail.id.in_(list(ids)), Mail.state == MailState.QUEUED).values(taken_by=run_id)
        )


async def delegated_mail_in(s: AsyncSession, session_id: str, states: Sequence[MailState]) -> list[Mail]:
    """会话里这些状态的委派来的信件，按到达顺序（结清没有不记在信上，由 sessions.settlement 问委派）。"""
    rows = await s.scalars(
        select(Mail)
        .where(Mail.session_id == session_id, Mail.delegation_id.is_not(None), Mail.state.in_(states))
        .order_by(Mail.id)
    )
    return list(rows.all())


async def consumed_by_in(s: AsyncSession, session_id: str, run_id: int) -> list[Mail]:
    """这次运行处理掉的信件：交给了它、已交给模型（带上会话条件：按会话的索引只扫这个会话的行）。"""
    rows = await s.scalars(
        select(Mail).where(
            Mail.session_id == session_id, Mail.taken_by == run_id, Mail.state == MailState.CONSUMED
        )
    )
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
    # 不带 RETURNING 的 DML 返回 CursorResult（有 rowcount），execute 的标注只写到 Result
    result = cast(
        CursorResult[Any],
        await s.execute(
            update(Mail)
            .where(Mail.id.in_(list(ids)), Mail.state == MailState.QUEUED)
            .values(state=MailState.CONSUMED, taken_by=run_id)
        ),
    )
    if result.rowcount != len(ids):
        raise RuntimeError(
            f"run {run_id} handed mail {list(ids)} to the model but only {result.rowcount} were still queued"
        )


async def discard_in(s: AsyncSession, session_id: str, mail: Sequence[Mail]) -> list[int]:
    """这些还没交给模型的信件记为被丢弃，不再处理（丢哪些由 sessions.settlement 的停止与最终失败定）。返回丢弃的
    信件 id。"""
    ids = [m.id for m in mail]
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


async def open_asks_in(s: AsyncSession, session_id: str) -> OpenAsks:
    """会话还没有结果、等用户的挂起调用：提问与等批准的调用（规则在 pending.open_asks_of）。"""
    queued = await queued_in(s, session_id)
    at = position(await since_last_response(s, session_id), await handed_out_in(s, session_id), queued)
    return open_asks_of(at, queued)


async def open_asks_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, OpenAsks]:
    """open_asks_in 的批量版本（会话列表的工作状态），同一条规则。"""
    tails = await since_last_response_among(s, session_ids)
    queued = await queued_among(s, session_ids)
    handed = await handed_out_among(s, session_ids)
    return {x: open_asks_of(position(tails[x], handed[x], queued[x]), queued[x]) for x in session_ids}
