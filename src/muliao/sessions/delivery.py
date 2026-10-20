"""交回结局：别的工作（后台任务、委派）把一项占用的结局交回会话，和释放占用同一个事务（docs/architecture.md 第 7 节
「占用与结清」）。占用已被停止清掉、或会话已删除时不投。唯一出口 deliver_in；后台任务经定向命令 PostEvent，委派由
thread 结清时在同一个事务里调用（sessions.delegation）。"""

from __future__ import annotations

import logging
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, TaskQueue
from muliao.sessions.events import PostEvent
from muliao.sessions.holds import get_in, release_in
from muliao.sessions.mailbox import post_in
from muliao.sessions.pending import AgentAnswer, AgentAnswered, pending_in
from muliao.sessions.tables import HoldStatus, Mail, MailKind, Session

log = logging.getLogger(__name__)


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
    source: str,
    event_text: str,
    answer: AgentAnswer | None = None,
) -> Delivered:
    """交回占用 holder 的结局并释放它，唯一出口。会话还停在这项占用对应的挂起调用上、且给了 answer 时作为那次调用
    的结果（回答信件）；否则作为事件信件（「迟到的回答改为事件」只在这里）。用户是否在等这封信随占用登记时记下的。
    按 key 幂等。"""
    if await s.get(Session, session_id) is None:
        log.info("outcome for a deleted session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.GONE
    hold = await get_in(s, session_id, holder)
    if hold is None:
        raise RuntimeError(f"session {session_id} has no hold {holder}")
    before = await release_in(s, session_id, holder)
    if before == HoldStatus.CLEARED:
        log.info("outcome for a stopped session dropped", extra={"session_id": session_id, "holder": holder})
        return Delivered.DROPPED
    if (
        before == HoldStatus.RELEASED
        and (await s.scalar(select(Mail.id).where(Mail.session_id == session_id, Mail.dedupe_key == key)))
        is None
    ):
        # 重放带的是同一个 key（按它去重）；换了 key 再交回一次，是同一项工作被交回了两次
        raise RuntimeError(f"hold {holder} of session {session_id} was already released by another delivery")
    if answer is not None and hold.call_id is not None and hold.call_id in await pending_in(s, session_id):
        await post_in(
            s,
            queue,
            session_id,
            kind=MailKind.ANSWER,
            content="",
            answers={hold.call_id: AgentAnswered.of(answer).model_dump()},
            awaited=hold.awaited,
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
            awaited=hold.awaited,
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
                source=command.source,
                event_text=command.content,
            )

        return commit

    messaging.handle(PostEvent, "sessions.post_event", handle)
