"""结清：会话自己判断委派来的工作（带委派的信件，mail.delegation_id）什么时候结束（docs/architecture.md 第 7 节
「占用与结清」），结局在同一个事务里交回发起方（sessions.delegation）。时机只有这几个：运行正常结束且工作做完
（sessions.finish）、运行最终失败（sessions.consumer.RunHandler.failed_in）、停止（sessions.halt）、删除
（sessions.deletion.delete_sessions）。

一项委派结清没有只看发起方引用它的占用是否还 active（sessions.thread_calls.owed_in）：
交回结局就释放占用，所以每项委派只交回一次；发起方已被停止（占用 cleared）或已删除（行随之删除、信上的
引用置空）时没有可交回的。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Attachment
from muliao.platform.tasks import TaskQueue, TaskView
from muliao.sessions.delegation import Ending, hand_back_in
from muliao.sessions.mailbox import delegated_mail_in, discard_in, queued_in
from muliao.sessions.runs import failure_of
from muliao.sessions.tables import Mail, MailState, Session
from muliao.sessions.thread_calls import Owed, owed_in

log = logging.getLogger(__name__)

# 交给会话、还没做完的信件所在的状态：排着的与已交给模型的
_LIVE = (MailState.QUEUED, MailState.CONSUMED)


async def _owed_by(s: AsyncSession, mail: Sequence[Mail]) -> list[Owed]:
    """这些信件带来的、还没结清的委派。"""
    return await owed_in(s, {m.delegation_id for m in mail if m.delegation_id is not None})


async def delegated_in(s: AsyncSession, session_id: str) -> bool:
    """会话有委派来、还没结清的工作（排着的或已交给模型的委派信件，发起方的占用还 active）：回答交给谁据此定
    （mailbox.audience_of）。"""
    return bool(await _owed_by(s, await delegated_mail_in(s, session_id, _LIVE)))


async def _settle_in(
    s: AsyncSession,
    queue: TaskQueue,
    session: Session,
    mail: Sequence[Mail],
    ending: Ending,
    text: str,
    attachments: Sequence[Attachment] = (),
) -> list[int]:
    """在调用方事务里把这些信件带来的、还没结清的委派的结局交回各自的发起方（sessions.delegation，交回即释放占用、
    即结清），返回结清的委派。"""
    owed = await _owed_by(s, mail)
    if not owed:
        return []
    settled = [o.delegation.id for o in owed]
    log.info(
        "session settled its delegations",
        extra={"session_id": session.id, "ending": ending, "delegations": settled},
    )
    await hand_back_in(s, queue, session, owed, ending, text, attachments)
    return settled


async def settle_finished_in(
    s: AsyncSession, queue: TaskQueue, session: Session, answer: str, attachments: Sequence[Attachment]
) -> list[int]:
    """运行正常结束且工作做完（sessions.finish）：已交给模型、还没结清的委派以「完成」结清，带上回答与附件。委派可能
    跨几次运行：停在挂起的调用上、或还有工作没做完时，由之后那次做完的运行结清。"""
    return await _settle_in(
        s,
        queue,
        session,
        await delegated_mail_in(s, session.id, [MailState.CONSUMED]),
        Ending.FINISHED,
        answer,
        attachments,
    )


async def settle_stopped_in(
    s: AsyncSession, queue: TaskQueue, session: Session, answer: str
) -> tuple[list[int], list[int]]:
    """用户停止（sessions.halt）：交给这个会话、还没结清的委派（排着的与已交给模型的）以「停止」结清，带上最近的
    回答；然后清空信箱（mailbox.discard_in）。对仍挂着的提问的回答已由停止写进历史、记为已处理（halt.close_history_in），
    不在信箱里。返回结清的委派与丢弃的信件。"""
    settled = await _settle_in(
        s, queue, session, await delegated_mail_in(s, session.id, _LIVE), Ending.STOPPED, answer
    )
    return settled, await discard_in(s, session.id, await queued_in(s, session.id))


async def settle_deleted_in(s: AsyncSession, queue: TaskQueue, session: Session) -> list[int]:
    """会话被删除（sessions.deletion）：交给它、还没结清的委派（排着的与已交给模型的）以「被删除」结清。"""
    return await _settle_in(
        s, queue, session, await delegated_mail_in(s, session.id, _LIVE), Ending.DELETED, ""
    )


async def settle_failure_in(s: AsyncSession, queue: TaskQueue, session: Session, run: TaskView) -> list[int]:
    """运行最终失败：交给这个会话、没能做完的委派以「失败」结清（发起方不会一直等）。按会话范围：已交给模型、还没结清
    的全部（包括早先停在挂起调用上的运行处理过的、被续跑接管的），加上交给了这次运行、还排着的（mail.taken_by）——
    运行在第一次保存之前就失败时，信箱里的信一直排着，也不会再有运行因为它们被唤醒（mailbox.wake_in）；它们随结清
    一起退出信箱，不会在之后的运行里被悄悄执行。运行开始之后才到的不动：它们会唤醒新的运行。只由队列记下最终失败时
    调用（RunHandler.failed_in）；它自己失败时不补做，见 docs/architecture.md「会话信箱」。"""
    queued = [m for m in await delegated_mail_in(s, session.id, [MailState.QUEUED]) if m.taken_by == run.id]
    consumed = await delegated_mail_in(s, session.id, [MailState.CONSUMED])
    settled = await _settle_in(s, queue, session, [*consumed, *queued], Ending.FAILED, failure_of(run))
    # 交给它的委派信件都退出信箱，不论这次是否交回了结局：已结清的（发起方被停止）也没人再要这项工作
    await discard_in(s, session.id, queued)
    return settled
