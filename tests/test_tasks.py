import asyncio
from datetime import timedelta

import anyio
import pytest
from sqlalchemy import text

from muliao.errors import Conflict
from muliao.notifications.service import DELIVER_TASK
from muliao.platform.db import utcnow
from muliao.platform.tables import EVENT_TASK, Task, TaskStatus
from muliao.platform.tasks import no_commit
from muliao.platform.worker import Worker
from muliao.reflection.service import REFLECTION_TASK
from muliao.sessions.lanes import RUN_TASK


async def _nothing(_s, _task) -> None:  # noqa: ANN001
    """这些测试只看队列本身的状态。"""


async def _enqueue(container, lane: str, agent: str):
    async with container.db.transaction() as s:
        return container.queue.enqueue(
            s, kind=DELIVER_TASK, lane=lane, agent_id=agent, session_id=None, payload={}
        )


async def test_lane_is_fifo_and_exclusive_while_other_lanes_run(container, make_agent):
    alpha = (await make_agent()).id
    a1 = await _enqueue(container, "a", alpha)
    a2 = await _enqueue(container, "a", alpha)
    b1 = await _enqueue(container, "b", alpha)
    q = container.queue

    first = await q.claim(container.worker.kinds)
    second = await q.claim(container.worker.kinds)
    assert (first.id, second.id) == (a1.id, b1.id)
    assert await q.claim(container.worker.kinds) is None  # a2 被 a1 阻塞

    await q.complete(first.id, no_commit)
    third = await q.claim(container.worker.kinds)
    assert third.id == a2.id


async def test_transient_failure_backs_off_then_fails_after_max_attempts(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    q = container.queue
    max_attempts = container.settings.runtime.task_max_attempts

    for attempt in range(1, max_attempts + 1):
        claimed = await q.claim(container.worker.kinds)
        if claimed is None:  # 退避尚未到期，直接把 not_before 拨到现在
            async with container.db.transaction() as s:
                from muliao.platform.tables import Task

                (await s.get(Task, task.id)).not_before = utcnow() - timedelta(seconds=1)
            claimed = await q.claim(container.worker.kinds)
        assert claimed.attempts == attempt
        failed = await q.fail(claimed.id, "boom", retryable=True, on_final=_nothing)

    assert failed.status == TaskStatus.FAILED
    assert failed.error == "boom"


async def test_non_retryable_failure_fails_immediately_and_can_be_retried_manually(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    q = container.queue
    await q.claim(container.worker.kinds)

    failed = await q.fail(task.id, "bug", retryable=False, on_final=_nothing)
    assert failed.status == TaskStatus.FAILED

    retried = await q.retry(task.id)
    assert retried.status == TaskStatus.PENDING and retried.attempts == 0
    assert (await q.claim(container.worker.kinds)).id == task.id


async def test_retry_is_refused_while_lane_has_newer_unfinished_work(container, make_agent):
    alpha = (await make_agent()).id
    q = container.queue
    old = await _enqueue(container, "a", alpha)
    await q.claim(container.worker.kinds)
    await q.fail(old.id, "bug", retryable=False, on_final=_nothing)
    await _enqueue(container, "a", alpha)

    with pytest.raises(Conflict):
        await q.retry(old.id)


async def test_recover_resets_running_tasks(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    await container.queue.claim(container.worker.kinds)

    assert [t.id for t in await container.queue.recover()] == [task.id]
    assert (await container.queue.claim(container.worker.kinds)).id == task.id


async def test_a_task_interrupted_past_its_attempts_is_failed_instead_of_recovered_forever(
    container, make_agent, caplog
):
    """任务自己让进程退出时（事故：agent 的 shell 命令重启了服务），恢复—重启—再恢复会无限循环下去，还一直占住它的
    lane：尝试次数用完就记为最终失败，钩子照常收尾，用户可手动重试。"""
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    settled: list[int] = []

    class Handler:
        async def execute(self, _task: Task):  # noqa: ANN202
            raise AssertionError("an exhausted task must not be executed again")

        async def failed_in(self, _s, failing: Task) -> None:  # noqa: ANN001
            settled.append(failing.id)

    async with container.db.transaction() as s:
        row = await s.get(Task, task.id)
        row.status, row.attempts = TaskStatus.RUNNING, container.queue.max_attempts

    worker = Worker(
        container.queue,
        {DELIVER_TASK: Handler()},
        container.bus,
        concurrency=1,
    )
    running = asyncio.create_task(worker.run_forever())
    try:
        async with asyncio.timeout(5):
            while True:
                await asyncio.sleep(0.02)
                async with container.db.transaction() as s:
                    row = await s.get(Task, task.id)
                    status, error = row.status, row.error
                if status != TaskStatus.RUNNING:
                    break
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    assert status == TaskStatus.FAILED and "attempts are used up" in error
    assert settled == [task.id]
    # 失败日志带的是这次失败本身，而不是此刻并不存在的「当前异常」（曾打出 NoneType: None）
    [failed] = [r for r in caplog.records if r.getMessage().startswith("task failed")]
    assert failed.exc_info is not None and "Interrupted" in caplog.text


async def test_a_worker_cancelled_while_claiming_keeps_its_slot_for_the_next_start(
    container, make_agent, monkeypatch
):
    """同一个 worker 停了再启动（测试的 drain 每次都这样）：停在领取中途时槽位曾经漏掉，漏够 concurrency 次后
    再启动的 worker 永远领不到任务（test_compaction 在并行跑测试时偶发超时）。"""
    alpha = (await make_agent()).id
    executed: list[int] = []
    done = asyncio.Event()

    class Handler:
        async def execute(self, task: Task):  # noqa: ANN202
            executed.append(task.id)
            done.set()
            return no_commit

        async def failed_in(self, _s, _task: Task) -> None:  # noqa: ANN001
            pass

    worker = Worker(container.queue, {DELIVER_TASK: Handler()}, container.bus, concurrency=1)
    claim = container.queue.claim
    claiming = asyncio.Event()

    async def stuck_claim(_kinds):  # noqa: ANN001, ANN202
        claiming.set()
        await anyio.sleep_forever()

    monkeypatch.setattr(container.queue, "claim", stuck_claim)
    running = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(claiming.wait(), 5)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)

    monkeypatch.setattr(container.queue, "claim", claim)
    task = await _enqueue(container, "a", alpha)
    running = asyncio.create_task(worker.run_forever())
    try:
        await asyncio.wait_for(done.wait(), 5)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert executed == [task.id]


async def test_a_worker_runs_at_most_once_at_a_time(container):
    """并发上限是每次 run_forever 各自的槽位：同时跑第二次会让上限翻倍，所以当场拒绝；第一次结束后可以再启动。"""
    import pytest

    worker = Worker(container.queue, {}, container.bus, concurrency=1)
    running = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    with pytest.raises(RuntimeError, match="already running"):
        await worker.run_forever()
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    again = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.05)
    assert not again.done()
    again.cancel()
    await asyncio.gather(again, return_exceptions=True)


async def test_retention_prunes_only_old_done_event_and_deliver_tasks(container, make_agent):
    """保留策略：7 天前完成的 event/deliver 删掉；failed、run、排队中的与较新的保留（为什么见 docs/architecture.md 3.2「保留」）。"""
    alpha = (await make_agent()).id
    old, recent = utcnow() - timedelta(days=8), utcnow() - timedelta(days=6)
    rows = {
        "old_event": (EVENT_TASK, TaskStatus.DONE, old),
        "old_deliver": (DELIVER_TASK, TaskStatus.DONE, old),
        "recent_event": (EVENT_TASK, TaskStatus.DONE, recent),
        "old_failed_event": (EVENT_TASK, TaskStatus.FAILED, old),
        "old_run": (RUN_TASK, TaskStatus.DONE, old),
        "old_reflection": (REFLECTION_TASK, TaskStatus.DONE, old),
        "pending_event": (EVENT_TASK, TaskStatus.PENDING, None),
    }
    ids = {}
    async with container.db.transaction() as s:
        for name, (kind, status, finished) in rows.items():
            task = Task(kind=kind, lane=f"l-{name}", agent_id=alpha, session_id=None, payload={})
            task.status, task.finished_at = status, finished
            s.add(task)
            await s.flush()
            ids[name] = task.id

    assert await container.retention.prune() == 2

    async with container.db.transaction() as s:
        left = {name for name, i in ids.items() if await s.get(Task, i) is not None}
    assert left == set(rows) - {"old_event", "old_deliver"}


async def test_mail_consumed_by_is_indexed_for_task_deletes(container):
    """删 tasks 时 SQLite 按外键 mail.consumed_by 查引用：没有索引就每删一行扫一遍 mail（#47 审查实测 mail 5 万行时
    删 6.7 万条要 135 秒）。"""
    async with container.db.transaction() as s:
        plan = (await s.execute(text("EXPLAIN QUERY PLAN SELECT 1 FROM mail WHERE consumed_by = 1"))).all()
    assert any("ix_mail_consumed_by" in row[-1] for row in plan), plan


async def test_startup_reports_unfinished_tasks_of_kinds_without_a_handler(container, make_agent, caplog):
    """任务种类是不透明字符串：库里留下的（例如改名前写入的）没有处理器的种类永远不会被领取、挡住 lane，启动时要
    报出来；有处理器的种类与已完成的不报。"""
    alpha = (await make_agent()).id
    async with container.db.transaction() as s:
        container.queue.enqueue(s, kind=RUN_TASK, lane="y", agent_id=alpha, session_id=None, payload={})
        # enqueue 已拒绝没有处理器的种类：旧行直接写进表
        s.add_all(
            [
                Task(
                    kind="renamed",
                    lane="x",
                    agent_id=alpha,
                    payload={},
                    status=TaskStatus.PENDING,
                    not_before=utcnow(),
                ),
                Task(
                    kind="gone",
                    lane="z",
                    agent_id=alpha,
                    payload={},
                    status=TaskStatus.DONE,
                    not_before=utcnow(),
                ),
            ]
        )
    await container._check_task_kinds()  # noqa: SLF001
    [record] = [r for r in caplog.records if "no handler" in r.message]
    assert record.levelname == "ERROR" and record.kinds == ["renamed"]


async def test_enqueueing_a_kind_without_a_handler_is_refused(container, make_agent):
    """没有处理器的 task 永远领不到，还挡住同一 lane 后面的 task：写入时当场拒绝，什么都不写。"""
    alpha = (await make_agent()).id
    with pytest.raises(RuntimeError, match="no handler is registered for task kind 'typo'"):
        async with container.db.transaction() as s:
            container.queue.enqueue(s, kind="typo", lane="x", agent_id=alpha, session_id=None, payload={})
    async with container.db.transaction() as s:
        assert (await s.execute(text("SELECT count(*) FROM tasks WHERE kind = 'typo'"))).scalar() == 0


def test_registering_a_task_kind_twice_is_refused():
    from muliao.platform.worker import register_handlers

    with pytest.raises(ValueError, match="'run' is registered twice"):
        register_handlers((RUN_TASK, object()), (EVENT_TASK, object()), (RUN_TASK, object()))
