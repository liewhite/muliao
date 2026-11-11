"""领域事件（platform.messaging）：在发布方的事务里按登记顺序调用订阅者，一起提交、一起回滚；漏登记在启动时报错。"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.notifications.tables import Notification
from muliao.platform.messaging import DomainEvent, Messaging


class Happened(DomainEvent):
    what: str


async def test_subscribers_run_in_the_publishers_transaction_in_order(container):
    messaging = Messaging()
    seen: list[str] = []

    async def first(s: AsyncSession, event: Happened) -> None:
        seen.append(f"first:{event.what}")
        s.add(Notification(title=event.what, body="b", session_id=None))

    async def second(_s: AsyncSession, event: Happened) -> None:
        seen.append(f"second:{event.what}")

    messaging.subscribe(Happened, first)
    messaging.subscribe(Happened, second)
    async with container.db.transaction() as s:
        await messaging.publish_in(s, Happened(what="x"))

    assert seen == ["first:x", "second:x"]
    async with container.db.transaction() as s:
        assert [n.title for n in (await s.scalars(select(Notification))).all()] == ["x"]


async def test_a_failing_subscriber_rolls_the_publishers_transaction_back(container):
    messaging = Messaging()

    async def writes(s: AsyncSession, _event: Happened) -> None:
        s.add(Notification(title="written", body="b", session_id=None))

    async def fails(_s: AsyncSession, _event: Happened) -> None:
        raise RuntimeError("subscriber broke")

    messaging.subscribe(Happened, writes)
    messaging.subscribe(Happened, fails)
    with pytest.raises(RuntimeError, match="subscriber broke"):
        async with container.db.transaction() as s:
            s.add(Notification(title="publisher", body="b", session_id=None))
            await messaging.publish_in(s, Happened(what="x"))

    async with container.db.transaction() as s:
        assert (await s.scalars(select(Notification))).all() == []


async def test_every_event_of_the_code_has_a_subscriber(container):
    # 组合根装配时已核对过一次（container 能建出来）；少登记一个就在启动时报错
    container.messaging.check_wired()
    with pytest.raises(RuntimeError, match="RunEnded"):
        Messaging().check_wired()
