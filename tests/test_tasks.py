from datetime import timedelta

from muliao.db import utcnow
from muliao.models import TaskKind, TaskStatus


async def _enqueue(container, lane: str, agent: str = "alpha"):
    async with container.db.transaction() as s:
        return container.queue.enqueue(
            s, kind=TaskKind.DELIVER, lane=lane, agent_name=agent, session_id=None, payload={}
        )


async def _noop(_s):
    return None


async def test_lane_is_fifo_and_exclusive_while_other_lanes_run(container, make_agent):
    await make_agent()
    a1 = await _enqueue(container, "a")
    a2 = await _enqueue(container, "a")
    b1 = await _enqueue(container, "b")
    q = container.queue

    first = await q.claim()
    second = await q.claim()
    assert (first.id, second.id) == (a1.id, b1.id)
    assert await q.claim() is None  # a2 被 a1 阻塞

    await q.complete(first.id, _noop)
    third = await q.claim()
    assert third.id == a2.id


async def test_transient_failure_backs_off_then_fails_after_max_attempts(container, make_agent):
    await make_agent()
    task = await _enqueue(container, "a")
    q = container.queue
    max_attempts = container.settings.task_max_attempts

    for attempt in range(1, max_attempts + 1):
        claimed = await q.claim()
        if claimed is None:  # 退避尚未到期，直接把 not_before 拨到现在
            async with container.db.transaction() as s:
                from muliao.models import Task

                (await s.get(Task, task.id)).not_before = utcnow() - timedelta(seconds=1)
            claimed = await q.claim()
        assert claimed.attempts == attempt
        failed = await q.fail(claimed.id, "boom", retryable=True)

    assert failed.status == TaskStatus.FAILED
    assert failed.error == "boom"


async def test_non_retryable_failure_fails_immediately_and_can_be_retried_manually(container, make_agent):
    await make_agent()
    task = await _enqueue(container, "a")
    q = container.queue
    await q.claim()

    failed = await q.fail(task.id, "bug", retryable=False)
    assert failed.status == TaskStatus.FAILED

    retried = await q.retry(task.id)
    assert retried.status == TaskStatus.PENDING and retried.attempts == 0
    assert (await q.claim()).id == task.id


async def test_recover_resets_running_tasks(container, make_agent):
    await make_agent()
    task = await _enqueue(container, "a")
    await container.queue.claim()

    assert await container.queue.recover() == 1
    assert (await container.queue.claim()).id == task.id
