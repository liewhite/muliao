"""收件箱：发给用户的通知（标题、正文、指向的会话、是否未读），notifications 表只在这里读写。通知页与应用角标的
未读数是它的展示；推到设备是 notifications.push 的事：写下通知时交给它排推送（schedule_in），推送时经 push_message_in
取内容，deliver task 只引用通知与一台设备。

Notifications 写下通知（notify、别的领域在自己事务里调的 notify_in、订阅 RunEnded 与 RunFailed），提供通知页与未读数，
随会话删除（订阅 SessionDeleted）；用户看过会话或 issue 时 HTTP 层直接调 mark_session_read / mark_issue_read。订阅都在发布方的事务里执行。

会话的未读不另存：有指向它的未读通知就是未读（unread_sessions）。需要用户看的只有这几种：用户在等的回答与提问
（RunEnded）、agent 主动通知（Notify，如 notify_user、issue 交给用户）、运行最终失败（RunFailed）。
"""

import logging
from collections.abc import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.notifications.events import NotificationEvent, Notify
from muliao.notifications.push import PushMessage, schedule_in
from muliao.notifications.tables import Notification
from muliao.platform.bus import EventBus
from muliao.platform.db import Database, when_committed_in
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import TaskQueue
from muliao.platform.text_edit import truncate
from muliao.sessions.api import session_in
from muliao.sessions.events import RunEnded, RunFailed, SessionDeleted

log = logging.getLogger(__name__)

# 运行失败的通知正文（错误原文）最多这么多字符，全文在任务页
FAILURE_BODY_MAX = 500


def _worth_pushing(event: RunEnded) -> bool:
    """停下来等用户批准的总是推：没人在看的运行（触发器、issue）也只有用户能批准。其余的要处理过的信件里有用户在等的、
    且有话要告诉用户：做完了，或停下来问用户；用户自己停止的、只在等别的 agent 的不推。"""
    waiting = set(event.waiting_on)
    if "approval" in waiting:
        return True
    return event.awaited and not event.stopped and (not waiting or "ask_user" in waiting)


class Notifications:
    def __init__(self, db: Database, queue: TaskQueue, bus: EventBus):
        self._db = db
        self._queue = queue
        self._bus = bus

    async def _enqueue_in(self, s: AsyncSession, agent_id: str | None, notification: Notification) -> None:
        """写下通知，交给推送给每台设备排一次推送。"""
        s.add(notification)
        await s.flush()
        created = NotificationEvent(notification_id=notification.id, session_id=notification.session_id)

        async def publish() -> None:
            self._bus.publish(created)

        when_committed_in(s, publish)
        await schedule_in(s, self._queue, notification.id, notification.session_id, agent_id)

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(RunEnded, self.on_run_ended)
        messaging.subscribe(RunFailed, self.on_run_failed)
        messaging.subscribe(SessionDeleted, self.on_session_deleted)

    async def notify_in(self, s: AsyncSession, request: Notify) -> None:
        """别的领域给用户发一条通知（并排上推送），在它自己的事务里（触发器的告警兜底、定时被挡、issue 交给用户）。"""
        await self._enqueue_in(
            s,
            request.agent_id,
            Notification(
                title=request.title,
                body=request.body,
                session_id=request.session_id,
                issue_id=request.issue_id,
            ),
        )

    async def on_run_ended(self, s: AsyncSession, event: RunEnded) -> None:
        """推送一次运行的回答（推送规则只在这里）。"""
        if not _worth_pushing(event):
            return
        await self._enqueue_in(
            s,
            event.agent_id,
            Notification(
                title=f"{event.agent_name} · {event.session_title}",
                body=event.answer,
                session_id=event.session_id,
            ),
        )

    async def on_run_failed(self, s: AsyncSession, event: RunFailed) -> None:
        """运行最终失败：通知用户，点开是那个会话；运行中途的工具报错不走这里。"""
        session = await session_in(s, event.session_id)
        if session is None:
            raise RuntimeError(
                f"run {event.run_id} failed in session {event.session_id}, which does not exist"
            )
        await self._enqueue_in(
            s,
            event.agent_id,
            Notification(
                title=f"{event.agent_name} · {session.title} 运行失败",
                body=truncate(event.error.strip(), FAILURE_BODY_MAX),
                session_id=event.session_id,
            ),
        )

    async def notify(self, agent_id: str | None, title: str, body: str, session_id: str | None) -> None:
        """agent_id 为 None 表示系统通知，不属于任何 agent；session_id 是点开通知打开的会话。"""
        async with self._db.transaction() as s:
            await self._enqueue_in(s, agent_id, Notification(title=title, body=body, session_id=session_id))

    async def send_test(self) -> None:
        """用户在界面上开启推送后发的测试通知（系统通知，不打开会话）。"""
        await self.notify(None, "Muliao", "推送通知已开启。", None)

    async def on_session_deleted(self, s: AsyncSession, event: SessionDeleted) -> None:
        """指向被删会话的通知随它删除。"""
        await s.execute(delete(Notification).where(Notification.session_id == event.session_id))

    async def mark_session_read(self, session_id: str) -> None:
        """用户看过了会话（打开它、看着它时又有了新回答）：指向它的通知标为已读（会话的未读就是这些通知）。"""
        async with self._db.transaction() as s:
            await s.execute(
                update(Notification)
                .where(Notification.session_id == session_id, Notification.unread.is_(True))
                .values(unread=False)
            )

    async def mark_issue_read(self, issue_id: int) -> None:
        """用户打开了 issue：请他审核这个 issue 的通知标为已读（与打开会话标记指向会话的通知一样）。"""
        async with self._db.transaction() as s:
            await s.execute(
                update(Notification)
                .where(Notification.issue_id == issue_id, Notification.unread.is_(True))
                .values(unread=False)
            )

    async def list(self, limit: int) -> list[Notification]:
        async with self._db.transaction() as s:
            query = select(Notification).order_by(Notification.id.desc()).limit(limit)
            return list((await s.scalars(query)).all())

    async def unread_sessions(self, session_ids: Sequence[str]) -> set[str]:
        """这些会话里未读的：有指向它的未读通知。"""
        async with self._db.transaction() as s:
            query = select(Notification.session_id).where(
                Notification.session_id.in_(session_ids), Notification.unread.is_(True)
            )
            return {
                sid for sid in (await s.scalars(query)).all() if sid is not None
            }  # 查询已限定 in_，不会有空

    async def unread_count(self) -> int:
        async with self._db.transaction() as s:
            return await _unread_count_in(s)

    async def mark_read(self, through_id: int) -> None:
        """标记到 through_id 为止的通知已读：之后才到的通知用户还没看到，保持未读。"""
        async with self._db.transaction() as s:
            await s.execute(
                update(Notification)
                .where(Notification.id <= through_id, Notification.unread.is_(True))
                .values(unread=False)
            )


async def _unread_count_in(s: AsyncSession) -> int:
    query = select(func.count()).select_from(Notification).where(Notification.unread.is_(True))
    return (await s.execute(query)).scalar_one()


async def push_message_in(s: AsyncSession, notification_id: int) -> PushMessage | None:
    """推送时的内容（deliver.DeliverHandler 用）：通知本身与此刻的未读数；通知已被删除时为 None。"""
    notification = await s.get(Notification, notification_id)
    if notification is None:
        return None
    return PushMessage(
        notification_id=notification.id,
        title=notification.title,
        body=notification.body,
        url=notification.url,
        session_id=notification.session_id,
        issue_id=notification.issue_id,
        unread=await _unread_count_in(s),
    )
