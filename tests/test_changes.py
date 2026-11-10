"""给前端的「哪个领域的数据变了」（schema.ChangedEvent）：写过表的事务提交后按领域发，平台改资源文件后发 agents。"""

from typing import get_args

import pytest

from muliao.notifications.tables import Notification
from muliao.platform.bus import Subscriber
from muliao.schema import ChangedEvent, Domain, _domains


def test_the_domain_literal_matches_the_tables():
    # 前端按这个字面量类型化；新加一个有表的领域要补进去
    assert set(get_args(Domain)) == set(_domains().values()) - {"platform"}


def _changed(sub: Subscriber) -> list[str]:
    events = []
    while not sub.queue.empty():
        e = sub.queue.get_nowait()
        if isinstance(e, ChangedEvent):
            events.append(e.resource)
    return events


async def test_committed_writes_announce_their_domain_and_rolled_back_or_read_only_ones_do_not(container):
    async with container.bus.subscribe() as sub:
        async with container.db.transaction() as s:
            s.add(Notification(title="t", body="b", session_id=None))
        assert _changed(sub) == ["notifications"]

        with pytest.raises(RuntimeError):
            async with container.db.transaction() as s:
                s.add(Notification(title="t", body="b", session_id=None))
                await s.flush()
                raise RuntimeError("boom")
        async with container.db.transaction() as s:
            await s.get(Notification, 1)
        assert _changed(sub) == []


async def test_writing_asset_files_announces_agents(container):
    async with container.bus.subscribe() as sub:
        version = (await container.memory.read("global")).version
        await container.memory.put("global", "- fact", actor="user", version=version)
        assert _changed(sub) == ["agents"]
