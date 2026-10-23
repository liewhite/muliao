"""通知：发给用户的一条消息（标题、正文、指向的会话、是否未读）。通知页与应用角标的未读数是它的展示；投递
（Web Push）是 delivery 的事，deliver task 只引用通知与一个推送订阅，内容只存在 notifications 表。

Notifications 是这个领域的唯一服务：写下通知并给每个推送订阅（设备）各排一个 deliver task（notify、定向命令 Notify、
订阅 RunEnded 与 RunFailed），通知页与未读数，随会话已读（订阅 SessionRead）与删除（订阅 SessionDeleted）。同一会话的事件在
同一条 lane 上（session_events_lane）。

会话的未读不另存：有指向它的未读通知就是未读（unread_sessions）。需要用户看的只有这几种：用户在等的回答与提问
（RunEnded）、agent 主动通知（Notify，如 notify_user、issue 交给用户）、运行最终失败（RunFailed）。
"""

import hashlib
import logging
from collections.abc import Sequence

from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.notifications.events import NotificationEvent, Notify
from muliao.notifications.tables import Notification, PushSubscription
from muliao.platform.bus import EventBus
from muliao.platform.db import Database
from muliao.platform.messaging import Delivery, Messaging
from muliao.platform.tasks import Commit, TaskQueue, no_commit
from muliao.sessions.api import policy_of, session_in
from muliao.sessions.events import RunEnded, RunFailed, SessionDeleted, SessionRead

log = logging.getLogger(__name__)

# 推送一条通知到一台设备的任务种类（组合根为它登记 DeliverHandler）
DELIVER_TASK = "deliver"


def deliver_lane(session_id: str | None, agent_id: str | None, endpoint: str) -> str:
    """投递通知的 lane：同一会话（没有会话时同一 agent、系统通知一条）推给同一台设备的按顺序；设备之间互不阻塞，
    一台在等重试不耽误别的设备。设备用 endpoint 的摘要表示：lane 会进日志，endpoint 本身是订阅凭据。"""
    device = hashlib.sha256(endpoint.encode()).hexdigest()[:12]
    return f"deliver:{session_id or agent_id or 'system'}:{device}"


def session_events_lane(event: RunEnded | RunFailed | SessionRead | SessionDeleted) -> str:
    """同一个会话的事件在同一条 lane 上按发布顺序处理：RunEnded、RunFailed 一定先于 SessionDeleted 发布（会话忙时不能删除），
    删除的清理之后不会再为它写通知；运行结束之后才打开会话时，SessionRead 排在 RunEnded 之后，标记已读能看到这次
    运行的通知。顺序只对没有失败的投递成立：人工重试的 RunEnded 由处理器检查会话还在。"""
    return f"notifications:{event.session_id}"


class Notifications:
    def __init__(self, db: Database, queue: TaskQueue, bus: EventBus):
        self._db = db
        self._queue = queue
        self._bus = bus

    async def _enqueue_in(self, s: AsyncSession, agent_id: str | None, notification: Notification) -> None:
        """写下通知，每个设备一个 deliver task：一台设备暂时失败只重试它自己，不会再推给已经送达的设备。"""
        s.add(notification)
        await s.flush()
        created = NotificationEvent(notification_id=notification.id, session_id=notification.session_id)
        event.listen(s.sync_session, "after_commit", lambda _s: self._bus.publish(created), once=True)
        endpoints = (await s.scalars(select(PushSubscription.endpoint))).all()
        if not endpoints:
            log.info(
                "no push subscriptions, notification not pushed",
                extra={"notification_id": notification.id, "session_id": notification.session_id},
            )
        for endpoint in endpoints:
            self._queue.enqueue(
                s,
                kind=DELIVER_TASK,
                lane=deliver_lane(notification.session_id, agent_id, endpoint),
                agent_id=agent_id,
                session_id=notification.session_id,
                payload={"notification_id": notification.id, "endpoint": endpoint},
            )

    def subscribe(self, messaging: Messaging) -> None:
        messaging.subscribe(RunEnded, "notifications.run_ended", self.on_run_ended, lane=session_events_lane)
        messaging.subscribe(
            RunFailed, "notifications.run_failed", self.on_run_failed, lane=session_events_lane
        )
        messaging.subscribe(
            SessionDeleted, "notifications.session_deleted", self.on_session_deleted, lane=session_events_lane
        )
        messaging.subscribe(
            SessionRead, "notifications.session_read", self.on_session_read, lane=session_events_lane
        )
        messaging.handle(Notify, "notifications.notify", self.on_notify)

    async def on_notify(self, command: Notify, _delivery: Delivery) -> Commit:
        async def commit(s: AsyncSession) -> None:
            await self._enqueue_in(
                s,
                command.agent_id,
                Notification(
                    title=command.title,
                    body=command.body,
                    session_id=command.session_id,
                    issue_id=command.issue_id,
                ),
            )

        return commit

    async def on_run_ended(self, event: RunEnded, _delivery: Delivery) -> Commit:
        """推送一次运行的回答：处理过的信件里有用户在等的，且有话要告诉用户（做完了，或停下来问用户）；用户自己停止的
        不推。"""
        if not event.awaited or event.stopped or (event.paused and not event.asked_user):
            return no_commit

        async def commit(s: AsyncSession) -> None:
            # 正常情况下同一会话的 SessionDeleted 排在后面（session_events_lane）；这条投递失败后被人工重试时可能已在
            # 删除之后。会话只会从存在变为删除，在本事务里检查没有竞态
            if await session_in(s, event.session_id) is None:
                log.info(
                    "run of a deleted session not notified",
                    extra={"session_id": event.session_id, "run_id": event.run_id},
                )
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

        return commit

    async def on_run_failed(self, event: RunFailed, _delivery: Delivery) -> Commit:
        """运行最终失败：列在会话列表里的会话都通知用户，点开是那个会话。私有 thread 的失败已作为结果交回发问的会话
        （由那里的 agent 处理），不单独通知；运行中途的工具报错不走这里。"""

        async def commit(s: AsyncSession) -> None:
            # 与 on_run_ended 同理：人工重试的投递可能已在会话删除之后
            session = await session_in(s, event.session_id)
            if session is None:
                log.info(
                    "failed run of a deleted session not notified",
                    extra={"session_id": event.session_id, "run_id": event.run_id},
                )
                return
            if not policy_of(session.kind).listed:
                return
            await self._enqueue_in(
                s,
                event.agent_id,
                Notification(
                    title=f"{event.agent_name} · {session.title} 运行失败",
                    body=event.summary,
                    session_id=event.session_id,
                ),
            )

        return commit

    async def notify(self, agent_id: str | None, title: str, body: str, session_id: str | None) -> None:
        """agent_id 为 None 表示系统通知，不属于任何 agent；session_id 是点开通知打开的会话。"""
        async with self._db.transaction() as s:
            await self._enqueue_in(s, agent_id, Notification(title=title, body=body, session_id=session_id))

    async def send_test(self) -> None:
        """用户在界面上开启推送后发的测试通知（系统通知，不打开会话）。"""
        await self.notify(None, "Muliao", "推送通知已开启。", None)

    async def on_session_deleted(self, event: SessionDeleted, _delivery: Delivery) -> Commit:
        """指向被删会话的通知随它删除。"""

        async def commit(s: AsyncSession) -> None:
            await s.execute(delete(Notification).where(Notification.session_id == event.session_id))

        return commit

    async def on_session_read(self, event: SessionRead, _delivery: Delivery) -> Commit:
        """看过了会话即看过了指向它的通知（会话已被删除时没有这样的通知，什么都不改）。"""

        async def commit(s: AsyncSession) -> None:
            await s.execute(
                update(Notification)
                .where(Notification.session_id == event.session_id, Notification.unread.is_(True))
                .values(unread=False)
            )

        return commit

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
            return set((await s.scalars(query)).all())

    async def unread_count(self) -> int:
        async with self._db.transaction() as s:
            return await unread_count_in(s)

    async def mark_read(self, through_id: int) -> None:
        """标记到 through_id 为止的通知已读：之后才到的通知用户还没看到，保持未读。"""
        async with self._db.transaction() as s:
            await s.execute(
                update(Notification)
                .where(Notification.id <= through_id, Notification.unread.is_(True))
                .values(unread=False)
            )


async def unread_count_in(s: AsyncSession) -> int:
    query = select(func.count()).select_from(Notification).where(Notification.unread.is_(True))
    return (await s.execute(query)).scalar_one()
