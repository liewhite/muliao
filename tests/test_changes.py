"""给前端的「哪个领域的数据变了」（schema.ChangedEvent）：写过表的事务提交后按领域发，平台改资源文件后发 agents。"""

from typing import get_args

import pytest

from muliao.api.events import derived_changes
from muliao.notifications.push import DELIVER_TASK
from muliao.notifications.tables import Notification
from muliao.platform.bus import Subscriber, TaskEvent
from muliao.platform.tables import TaskStatus
from muliao.schema import ChangedEvent, Domain, _domains
from muliao.sessions.api import RUN_TASK


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


def _task(kind: str, status: TaskStatus) -> TaskEvent:
    return TaskEvent(task_id=1, kind=kind, status=status, session_id=None, agent=None, error=None)


def test_derived_views_are_announced_after_their_source():
    def derived(e) -> list[str]:
        return [c.resource for c in derived_changes(e)]

    # 工作状态由任务派生（会话列表、issue 看板），任务列表本身也变了
    assert derived(_task(RUN_TASK, TaskStatus.RUNNING)) == ["tasks", "sessions", "issues"]
    assert derived(_task(DELIVER_TASK, TaskStatus.DONE)) == ["tasks", "sessions", "issues"]
    # 运行结束时 agent 可能在运行里改过资源文件
    assert derived(_task(RUN_TASK, TaskStatus.FAILED)) == ["tasks", "sessions", "issues", "agents"]
    # 会话的未读由通知派生
    assert derived(ChangedEvent(resource="notifications")) == ["sessions"]
    # issue 上的工作状态与 issue 会话由会话派生
    assert derived(ChangedEvent(resource="sessions")) == ["issues"]
    assert derived(ChangedEvent(resource="issues")) == []


async def test_writing_asset_files_announces_agents(container):
    async with container.bus.subscribe() as sub:
        version = (await container.memory.read("global")).version
        await container.memory.put("global", "- fact", version=version)
        assert _changed(sub) == ["agents"]
