"""结清：会话自己判断委派来的工作（带关联 id 的信件）什么时候结束（docs/architecture.md 第 7 节「占用与结清」），
结局在同一个事务里交回发起方（sessions.delegation）。时机只有这几个：运行正常结束且工作做完（sessions.finish）、
运行最终失败（sessions.consumer.RunHandler.failed_in）、停止（sessions.halt）、删除
（sessions.deletion.delete_sessions）。每个关联只结清一次（mail.correlation_settled）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Attachment
from muliao.platform.db import Database
from muliao.platform.tables import Task, TaskStatus
from muliao.platform.tasks import TaskQueue
from muliao.sessions.delegation import Ending, hand_back_in
from muliao.sessions.mailbox import discard_in
from muliao.sessions.runs import failure_of, handed_to, latest_run
from muliao.sessions.tables import Mail, MailState, Session

log = logging.getLogger(__name__)


async def _unsettled_in(s: AsyncSession, session_id: str, states: Sequence[MailState]) -> list[Mail]:
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


async def _settle_in(
    s: AsyncSession,
    queue: TaskQueue,
    session: Session,
    mail: Sequence[Mail],
    ending: Ending,
    text: str,
    attachments: Sequence[Attachment] = (),
) -> list[str]:
    """在调用方事务里结清这些信件带来的关联，并在同一个事务里把结局交回各自的发起方（sessions.delegation），返回
    结清的关联。"""
    correlations = list(
        dict.fromkeys(m.correlation for m in mail if m.correlation and not m.correlation_settled)
    )
    for m in mail:
        if m.correlation:
            m.correlation_settled = True
    if not correlations:
        return []
    log.info(
        "session settled its correlations",
        extra={"session_id": session.id, "ending": ending, "correlations": correlations},
    )
    await hand_back_in(s, queue, session, correlations, ending, text, attachments)
    return correlations


async def settle_finished_in(
    s: AsyncSession, queue: TaskQueue, session: Session, answer: str, attachments: Sequence[Attachment]
) -> list[str]:
    """运行正常结束且工作做完（sessions.finish）：已交给模型、还没结清的委派以「完成」结清，带上回答与附件。委派可能
    跨几次运行：停在挂起的调用上、或还有工作没做完时，由之后那次做完的运行结清。"""
    return await _settle_in(
        s,
        queue,
        session,
        await _unsettled_in(s, session.id, [MailState.CONSUMED]),
        Ending.FINISHED,
        answer,
        attachments,
    )


async def settle_stopped_in(
    s: AsyncSession, queue: TaskQueue, session: Session, answer: str
) -> tuple[list[str], list[int]]:
    """用户停止（sessions.halt）：交给这个会话、还没结清的委派（排着的与已交给模型的）以「停止」结清，带上最近的
    回答；然后清空信箱（mailbox.discard_in）。返回结清的关联与丢弃的信件。"""
    settled = await _settle_in(
        s,
        queue,
        session,
        await _unsettled_in(s, session.id, [MailState.QUEUED, MailState.CONSUMED]),
        Ending.STOPPED,
        answer,
    )
    return settled, await discard_in(s, session.id)


async def settle_deleted_in(s: AsyncSession, queue: TaskQueue, session: Session) -> list[str]:
    """会话被删除（sessions.deletion）：交给它、还没结清的委派（排着的与已交给模型的）以「被删除」结清。"""
    return await _settle_in(
        s,
        queue,
        session,
        await _unsettled_in(s, session.id, [MailState.QUEUED, MailState.CONSUMED]),
        Ending.DELETED,
        "",
    )


async def settle_failure_in(s: AsyncSession, queue: TaskQueue, session: Session, run: Task) -> list[str]:
    """运行最终失败：交给这个会话、没能做完的委派以「失败」结清（发起方不会一直等）。按会话范围：已交给模型、还没结清
    的全部（包括早先停在挂起调用上的运行处理过的、被续跑接管的），加上交给过这次运行、还排着的——运行在第一次保存
    之前就失败时，开头取走的信件一直排着，也不会再有运行因为它们被唤醒（mailbox.handed_to）；它们随结清一起退出
    信箱，不会在之后的运行里被悄悄执行。运行开始之后才到的不动：它们会唤醒新的运行。

    例外：收尾钩子失败、对账补做之前又有新信件到达，投信当场唤醒的新运行会把这些信件一起取走执行；它们之后随那次
    运行的正常结清交回，发起方照样拿到结局。

    两条执行路径共用这一个函数：队列记下最终失败时（RunHandler.failed_in），以及它没做成时信箱对账补做
    （reconcile_failures）。"""
    stranded = [m for m in await _unsettled_in(s, session.id, [MailState.QUEUED]) if handed_to(m, run)]
    settled = await _settle_in(
        s,
        queue,
        session,
        [*await _unsettled_in(s, session.id, [MailState.CONSUMED]), *stranded],
        Ending.FAILED,
        failure_of(run),
    )
    await discard_in(s, session.id, stranded)
    return settled


async def reconcile_failures(db: Database, queue: TaskQueue) -> list[str]:
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
                settled = await settle_failure_in(s, queue, session, run)
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
