"""会话的结清与结局的交回（会话领域，docs/architecture.md 第 7 节）。

- 结清：会话自己判断委派来的工作（带关联 id 的信件）什么时候结束，发布 SessionSettled。时机只有这几个：运行正常
  结束且工作做完（sessions.finish）、运行最终失败（sessions.consumer.RunHandler.failed_in）、停止（sessions.halt）、
  删除（sessions.conversation.delete_sessions）。每个关联只结清一次（mail.correlation_settled）。
- 交回：别的领域把一项占用的结局交回会话，和释放占用同一个事务。占用已被停止清掉、或会话已删除时不投。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Attachment
from muliao.agents.api import agent_in
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tasks import Commit, TaskQueue
from muliao.sessions.events import Ending, PostEvent, SessionSettled
from muliao.sessions.holds import get_in, release_in
from muliao.sessions.mailbox import discard_in, handed_to, latest_run, post_in
from muliao.sessions.pending import AgentAnswer, AgentAnswered, pending_in
from muliao.sessions.tables import HoldStatus, Mail, MailKind, MailState, Session

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


async def settle_in(
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
    agent = await agent_in(s, session.agent_id)
    messaging.publish_in(
        s,
        SessionSettled(
            session_id=session.id,
            agent_id=session.agent_id,
            agent_name=agent.name,
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


def failure_of(run: Task) -> str:
    """失败的运行的错误说明；队列契约保证 failed 一定带 error。"""
    if run.status != TaskStatus.FAILED or run.error is None:
        raise RuntimeError(
            f"run {run.id} is {run.status} with error {run.error!r}, expected a recorded failure"
        )
    return run.error


async def settle_failure_in(s: AsyncSession, messaging: Messaging, session: Session, run: Task) -> list[str]:
    """运行最终失败：交给这个会话、没能做完的委派以「失败」结清（发起方不会一直等）。按会话范围：已交给模型、还没结清
    的全部（包括早先停在挂起调用上的运行处理过的、被续跑接管的），加上交给过这次运行、还排着的——运行在第一次保存
    之前就失败时，开头取走的信件一直排着，也不会再有运行因为它们被唤醒（mailbox.handed_to）；它们随结清一起退出
    信箱，不会在之后的运行里被悄悄执行。运行开始之后才到的不动：它们会唤醒新的运行。

    例外：收尾钩子失败、对账补做之前又有新信件到达，投信当场唤醒的新运行会把这些信件一起取走执行；它们之后随那次
    运行的正常结清交回，发起方照样拿到结局。

    两条执行路径共用这一个函数：队列记下最终失败时（RunHandler.failed_in），以及它没做成时信箱对账补做
    （reconcile_failures）。"""
    stranded = [m for m in await unsettled_in(s, session.id, [MailState.QUEUED]) if handed_to(m, run)]
    settled = await settle_in(
        s,
        messaging,
        session,
        [*await unsettled_in(s, session.id, [MailState.CONSUMED]), *stranded],
        Ending.FAILED,
        failure_of(run),
    )
    await discard_in(s, session.id, stranded)
    return settled


async def reconcile_failures(db: Database, messaging: Messaging) -> list[str]:
    """failed_in 没做成（收尾钩子自己失败，worker 不带钩子记下了失败）时补做：最近一次运行已失败、仍有还没结清的关联
    的会话（已交给模型的，或交给过那次运行、还排着的），按 settle_failure_in 结清——候选范围与它的范围一致，由它按
    同一条规则筛选。返回确实结清了关联的会话。

    每个会话一个事务、各自的异常边界：钩子失败多半是确定性的 bug，补做时会再撞上它；它只记 error（带会话与运行），
    不回滚别的会话的补做，也不让对账循环崩溃、把整个服务拖进重启循环。"""
    async with db.transaction() as s:
        candidates = (
            await s.scalars(
                select(Mail.session_id)
                .where(
                    Mail.correlation.is_not(None),
                    Mail.correlation_settled.is_(False),
                    Mail.state.in_([MailState.CONSUMED, MailState.QUEUED]),
                )
                .distinct()
            )
        ).all()
    done = []
    for session_id in candidates:
        run_id = None
        try:
            async with db.transaction() as s:
                run = await latest_run(s, session_id)
                session = await s.get(Session, session_id)
                if run is None or run.status != TaskStatus.FAILED or session is None:
                    continue
                run_id = run.id
                settled = await settle_failure_in(s, messaging, session, run)
        except Exception:
            log.exception(
                "settling the delegations of a failed run failed again",
                extra={"session_id": session_id, "run_id": run_id},
            )
            continue
        if settled:
            # 排着的只是失败之后才到的信件时什么都不结清：它们会唤醒新的运行
            done.append(session_id)
    return done


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
