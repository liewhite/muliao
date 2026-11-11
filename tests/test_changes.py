"""给前端的「哪类数据变了」（schema.ChangedEvent）：写过表的事务提交后按类别发，资源文件有改动时发 agents。"""

from typing import get_args

import anyio
import pytest

from muliao.agents.source import visible_change
from muliao.notifications.push import DELIVER_TASK
from muliao.notifications.tables import Notification
from muliao.platform.bus import Subscriber
from muliao.platform.fs import write_atomic
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


async def test_queued_tasks_announce_tasks(container):
    # 任务列表、会话与 issue 上的工作状态读任务表（schema.resource_of）
    async with container.bus.subscribe() as sub:
        async with container.db.transaction() as s:
            s.add(Notification(title="t", body="b", session_id=None))
            container.queue.enqueue(
                s, kind=DELIVER_TASK, lane="test", agent_id=None, session_id=None, payload={}
            )
        assert _changed(sub) == ["notifications", "tasks"]


async def test_any_change_to_asset_files_announces_agents(container):
    """不管谁改的（平台的写入口、agent 的 shell、用户在别处），资源文件变了就以 agents 发。"""
    # 平台自己的写入口是原子写（隐藏的临时文件 rename 过去），也要报出来
    notes = container.settings.assets_dir / "platform" / "memory" / "notes.md"
    async with container.bus.subscribe() as sub, anyio.create_task_group() as tg:
        tg.start_soon(container.assets.watch)
        seen: list[str] = []
        # 监听什么时候开始生效看不到：一直改到收到为止
        with anyio.fail_after(20):
            while not seen:
                write_atomic(notes, "- fact")
                await anyio.sleep(0.3)
                seen = _changed(sub)
        assert set(seen) == {"agents"}
        tg.cancel_scope.cancel()


def test_hidden_files_are_not_asset_changes(tmp_path):
    assert visible_change(tmp_path, str(tmp_path / "platform" / "memory" / "notes.md"))
    assert not visible_change(tmp_path, str(tmp_path / "platform" / "memory" / ".notes.md.tmp1"))
    assert not visible_change(tmp_path, str(tmp_path / "platform" / "skills" / "s" / ".venv" / "x.py"))
    assert visible_change(tmp_path, "/elsewhere/x")
