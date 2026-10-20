from datetime import timedelta

import pytest

from muliao.errors import Conflict
from muliao.models import TaskKind, TaskStatus
from muliao.platform.db import utcnow
from muliao.platform.tasks import no_commit


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
                from muliao.models import Task

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
