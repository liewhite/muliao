import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text

from muliao.errors import Conflict
from muliao.platform.db import utcnow
from muliao.platform.tables import Task, TaskKind, TaskStatus
from muliao.platform.tasks import no_commit
from muliao.platform.worker import Worker


async def _nothing(_s, _task) -> None:  # noqa: ANN001
    """这些测试只看队列本身的状态。"""


async def _enqueue(container, lane: str, agent: str):
    async with container.db.transaction() as s:
        return container.queue.enqueue(
            s, kind=TaskKind.DELIVER, lane=lane, agent_id=agent, session_id=None, payload={}
        )


async def test_lane_is_fifo_and_exclusive_while_other_lanes_run(container, make_agent):
    alpha = (await make_agent()).id
    a1 = await _enqueue(container, "a", alpha)
    a2 = await _enqueue(container, "a", alpha)
    b1 = await _enqueue(container, "b", alpha)
    q = container.queue

    first = await q.claim(list(TaskKind))
    second = await q.claim(list(TaskKind))
    assert (first.id, second.id) == (a1.id, b1.id)
    assert await q.claim(list(TaskKind)) is None  # a2 被 a1 阻塞

    await q.complete(first.id, no_commit)
    third = await q.claim(list(TaskKind))
    assert third.id == a2.id


async def test_transient_failure_backs_off_then_fails_after_max_attempts(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    q = container.queue
    max_attempts = container.settings.runtime.task_max_attempts

    for attempt in range(1, max_attempts + 1):
        claimed = await q.claim(list(TaskKind))
        if claimed is None:  # 退避尚未到期，直接把 not_before 拨到现在
            async with container.db.transaction() as s:
                from muliao.platform.tables import Task

                (await s.get(Task, task.id)).not_before = utcnow() - timedelta(seconds=1)
            claimed = await q.claim(list(TaskKind))
        assert claimed.attempts == attempt
        failed = await q.fail(claimed.id, "boom", retryable=True, on_final=_nothing)

    assert failed.status == TaskStatus.FAILED
    assert failed.error == "boom"


async def test_non_retryable_failure_fails_immediately_and_can_be_retried_manually(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    q = container.queue
    await q.claim(list(TaskKind))

    failed = await q.fail(task.id, "bug", retryable=False, on_final=_nothing)
    assert failed.status == TaskStatus.FAILED

    retried = await q.retry(task.id)
    assert retried.status == TaskStatus.PENDING and retried.attempts == 0
    assert (await q.claim(list(TaskKind))).id == task.id


async def test_retry_is_refused_while_lane_has_newer_unfinished_work(container, make_agent):
    alpha = (await make_agent()).id
    q = container.queue
    old = await _enqueue(container, "a", alpha)
    await q.claim(list(TaskKind))
    await q.fail(old.id, "bug", retryable=False, on_final=_nothing)
    await _enqueue(container, "a", alpha)

    with pytest.raises(Conflict):
        await q.retry(old.id)


async def test_recover_resets_running_tasks(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    await container.queue.claim(list(TaskKind))

    assert [t.id for t in await container.queue.recover()] == [task.id]
    assert (await container.queue.claim(list(TaskKind))).id == task.id


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
        {TaskKind.DELIVER: Handler()},
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


async def test_retention_prunes_only_old_done_event_and_deliver_tasks(container, make_agent):
    """保留策略：7 天前完成的 event/deliver 删掉；failed、run、排队中的与较新的保留（为什么见 docs/architecture.md 3.2「保留」）。"""
    alpha = (await make_agent()).id
    old, recent = utcnow() - timedelta(days=8), utcnow() - timedelta(days=6)
    rows = {
        "old_event": (TaskKind.EVENT, TaskStatus.DONE, old),
        "old_deliver": (TaskKind.DELIVER, TaskStatus.DONE, old),
        "recent_event": (TaskKind.EVENT, TaskStatus.DONE, recent),
        "old_failed_event": (TaskKind.EVENT, TaskStatus.FAILED, old),
        "old_run": (TaskKind.RUN, TaskStatus.DONE, old),
        "old_reflection": (TaskKind.REFLECTION, TaskStatus.DONE, old),
        "pending_event": (TaskKind.EVENT, TaskStatus.PENDING, None),
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
