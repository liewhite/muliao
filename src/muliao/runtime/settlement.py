"""会话的结清与结局的交回（会话领域，docs/domain-design.md 4.1）。

- 结清：会话自己判断委派来的工作（带关联 id 的信件）什么时候结束，发布 SessionSettled。时机只有这几个：运行正常
  结束且工作做完（runtime.finish）、运行最终失败（runtime.consumer.RunHandler.failed_in）、停止（runtime.halt）、
  删除（runtime.conversation.delete_sessions）。每个关联只结清一次（mail.correlation_settled）。
- 交回：别的领域把一项占用的结局交回会话，和释放占用同一个事务。占用已被停止清掉、或会话已删除时不投。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.conventions import Attachment
from muliao.models import HoldStatus, Mail, MailKind, MailState, Session
from muliao.platform.messaging import Delivery, Messaging
from muliao.runtime.holds import get_in, release_in
from muliao.runtime.mailbox import post_in
from muliao.runtime.pending import AgentAnswer, pending_in
from muliao.runtime.session_events import Ending, PostEvent, SessionSettled
from muliao.runtime.tasks import Commit, TaskQueue

log = logging.getLogger(__name__)


async def unsettled_in(s: AsyncSession, session_id: str, states: Sequence[MailState]) -> list[Mail]:
    """会话里带关联、还没结清的信件。"""
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


def settle_in(
    s: AsyncSession,
    messaging: Messaging,
    session: Session,
    mail: Sequence[Mail],
    ending: Ending,
    text: str,
    attachments: Sequence[Attachment] = (),
) -> list[str]:
    """在调用方事务里结清这些信件带来的关联并发布 SessionSettled，返回结清的关联。"""
    correlations = list(
        dict.fromkeys(m.correlation for m in mail if m.correlation and not m.correlation_settled)
    )
    for m in mail:
        if m.correlation:
            m.correlation_settled = True
    if not correlations:
        return []
    if session.thread is None:
        raise RuntimeError(f"session {session.id} carries correlations {correlations} but is not a thread")
    messaging.publish_in(
        s,
        SessionSettled(
            session_id=session.id,
            agent_id=session.agent_id,
            thread=session.thread,
            ending=ending,
            text=text,
            attachments=list(attachments),
            correlations=correlations,
        ),
    )
    log.info(
        "session settled its correlations",
        extra={"session_id": session.id, "ending": ending, "correlations": correlations},
    )
    return correlations


class Delivered(StrEnum):
    """交回结局的结果（命令的结果类型，契约内的情况不靠异常表达）。"""

    # 作为挂起调用的结果（回答信件）
    ANSWER = "answer"
    # 作为事件信件
    EVENT = "event"
    # 占用已被停止清掉：不投
    DROPPED = "dropped"
    # 会话已删除
    GONE = "gone"


async def deliver_in(
    s: AsyncSession,
    queue: TaskQueue,
    session_id: str,
    holder: str,
    *,
    key: str,
    awaited: bool,
    source: str,
    event_text: str,
    answer: AgentAnswer | None = None,
) -> Delivered:
    """交回占用 holder 的结局并释放它，唯一出口。会话还停在这项占用对应的挂起调用上、且给了 answer 时作为那次调用
    的结果（回答信件）；否则作为事件信件（「迟到的回答改为事件」只在这里）。按 key 幂等。"""
    if await s.get(Session, session_id) is None:
        log.info("outcome for a deleted session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.GONE
    hold = await get_in(s, session_id, holder)
    if hold is None:
        raise RuntimeError(f"session {session_id} has no hold {holder}")
    if await release_in(s, session_id, holder) == HoldStatus.CLEARED:
        log.info("outcome for a stopped session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.DROPPED
    if answer is not None and hold.call_id is not None and hold.call_id in await pending_in(s, session_id):
        await post_in(
            s,
            queue,
            session_id,
            kind=MailKind.ANSWER,
            content="",
            answers={hold.call_id: answer.model_dump()},
            awaited=awaited,
            dedupe_key=key,
        )
        delivered = Delivered.ANSWER
    else:
        await post_in(
            s,
            queue,
            session_id,
            kind=MailKind.EVENT,
            source=source,
            content=event_text,
            awaited=awaited,
            dedupe_key=key,
        )
        delivered = Delivered.EVENT
    log.info(
        "outcome delivered",
        extra={"session_id": session_id, "holder": holder, "delivered_as": delivered, "key": key},
    )
    return delivered


def handle_post_event(messaging: Messaging, queue: TaskQueue) -> None:
    """登记定向命令 PostEvent 的处理者（会话领域）：投信与释放占用和这条投递的完成在同一个事务里。"""

    async def handle(command: PostEvent, _delivery: Delivery) -> Commit:
        async def commit(s: AsyncSession) -> None:
            await deliver_in(
                s,
                queue,
                command.session_id,
                command.release,
                key=command.key,
                awaited=command.awaited,
                source=command.source,
                event_text=command.content,
            )

        return commit

    messaging.handle(PostEvent, "sessions.post_event", handle)
