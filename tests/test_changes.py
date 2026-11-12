"""给前端的「数据变了」（platform.bus.ChangedEvent）：写过表的事务提交后发，资源文件有改动时也发。"""

import anyio
import pytest

from muliao.agents.source import visible_change
from muliao.notifications.push import DELIVER_TASK
from muliao.notifications.tables import Notification
from muliao.platform.bus import ChangedEvent, Subscriber
from muliao.platform.fs import write_atomic


def _changed(sub: Subscriber) -> int:
    """收到了几次「数据变了」"""
    count = 0
    while not sub.queue.empty():
        count += isinstance(sub.queue.get_nowait(), ChangedEvent)
    return count


async def test_committed_writes_announce_a_change_and_rolled_back_or_read_only_ones_do_not(container):
    async with container.bus.subscribe() as sub:
        async with container.db.transaction() as s:
            s.add(Notification(title="t", body="b", session_id=None))
        assert _changed(sub) == 1

        with pytest.raises(RuntimeError):
            async with container.db.transaction() as s:
                s.add(Notification(title="t", body="b", session_id=None))
                await s.flush()
                raise RuntimeError("boom")
        async with container.db.transaction() as s:
            await s.get(Notification, 1)
        assert _changed(sub) == 0


async def test_one_change_per_transaction_whatever_it_wrote(container):
    async with container.bus.subscribe() as sub:
        async with container.db.transaction() as s:
            s.add(Notification(title="t", body="b", session_id=None))
            container.queue.enqueue(
                s, kind=DELIVER_TASK, lane="test", agent_id=None, session_id=None, payload={}
            )
        assert _changed(sub) == 1


async def test_any_change_to_asset_files_announces_a_change(container):
    """不管谁改的（平台的写入口、agent 的 shell、用户在别处），资源文件变了就发。"""
    # 平台自己的写入口是原子写（隐藏的临时文件 rename 过去），也要报出来
    notes = container.settings.assets_dir / "platform" / "memory" / "notes.md"
    async with container.bus.subscribe() as sub, anyio.create_task_group() as tg:
        tg.start_soon(container.assets.watch)
        seen = 0
        # 监听什么时候开始生效看不到：一直改到收到为止
        with anyio.fail_after(20):
            while not seen:
                write_atomic(notes, "- fact")
                await anyio.sleep(0.3)
                seen = _changed(sub)
        tg.cancel_scope.cancel()


def test_hidden_files_are_not_asset_changes(tmp_path):
    assert visible_change(tmp_path, str(tmp_path / "platform" / "memory" / "notes.md"))
    assert not visible_change(tmp_path, str(tmp_path / "platform" / "memory" / ".notes.md.tmp1"))
    assert not visible_change(tmp_path, str(tmp_path / "platform" / "skills" / "s" / ".venv" / "x.py"))
    assert visible_change(tmp_path, "/elsewhere/x")
