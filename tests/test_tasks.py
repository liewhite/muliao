import asyncio
from datetime import timedelta

import anyio
import pytest
from sqlalchemy import select, text

from muliao.errors import Conflict
from muliao.notifications.service import DELIVER_TASK
from muliao.platform.db import utcnow
from muliao.platform.tables import EVENT_TASK, Task, TaskStatus
from muliao.platform.tasks import no_commit
from muliao.platform.worker import Worker
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

    first = await q.claim(container.queue.kinds)
    second = await q.claim(container.queue.kinds)
    assert (first.id, second.id) == (a1.id, b1.id)
    assert await q.claim(container.queue.kinds) is None  # a2 被 a1 阻塞

    await q.complete(first.id, no_commit)
    third = await q.claim(container.queue.kinds)
    assert third.id == a2.id


async def test_transient_failure_backs_off_then_fails_after_max_attempts(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    q = container.queue
    max_attempts = container.settings.runtime.task_max_attempts

    for attempt in range(1, max_attempts + 1):
        claimed = await q.claim(container.queue.kinds)
        if claimed is None:  # 退避尚未到期，直接把 not_before 拨到现在
            async with container.db.transaction() as s:
                from muliao.platform.tables import Task

                (await s.get(Task, task.id)).not_before = utcnow() - timedelta(seconds=1)
            claimed = await q.claim(container.queue.kinds)
        assert claimed.attempts == attempt
        failed = await q.fail(claimed.id, "boom", retry=q.retries.transient, on_final=_nothing)

    assert failed.status == TaskStatus.FAILED
    assert failed.error == "boom"


async def test_non_retryable_failure_fails_immediately_and_can_be_retried_manually(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    q = container.queue
    await q.claim(container.queue.kinds)

    failed = await q.fail(task.id, "bug", retry=None, on_final=_nothing)
    assert failed.status == TaskStatus.FAILED

    retried = await q.retry(task.id)
    assert retried.status == TaskStatus.PENDING and retried.attempts == 0
    assert (await q.claim(container.queue.kinds)).id == task.id


async def test_retry_is_refused_while_lane_has_newer_unfinished_work(container, make_agent):
    alpha = (await make_agent()).id
    q = container.queue
    old = await _enqueue(container, "a", alpha)
    await q.claim(container.queue.kinds)
    await q.fail(old.id, "bug", retry=None, on_final=_nothing)
    await _enqueue(container, "a", alpha)

    with pytest.raises(Conflict):
        await q.retry(old.id)


async def test_recover_resets_running_tasks(container, make_agent):
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    await container.queue.claim(container.queue.kinds)

    assert [t.id for t in await container.queue.recover(container.queue.kinds)] == [task.id]
    assert (await container.queue.claim(container.queue.kinds)).id == task.id


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
        row.status, row.attempts = TaskStatus.RUNNING, container.queue.retries.max_attempts

    worker = Worker(
        container.queue,
        {DELIVER_TASK: Handler()},
        container.bus,
    )
    await worker.recover()
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
        "old_event": (EVENT_TASK, TaskStatus.DONE, old),
        "old_deliver": (DELIVER_TASK, TaskStatus.DONE, old),
        "recent_event": (EVENT_TASK, TaskStatus.DONE, recent),
        "old_failed_event": (EVENT_TASK, TaskStatus.FAILED, old),
        "old_run": (RUN_TASK, TaskStatus.DONE, old),
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
    await container.queue.check_kinds()
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


async def test_recover_resets_only_the_kinds_asked_for(container, make_agent):
    """按种类恢复：只复位给定的种类（worker 只恢复有处理器的种类，用完尝试次数的要由处理器收尾）。"""
    alpha = (await make_agent()).id
    async with container.db.transaction() as s:
        run = container.queue.enqueue(s, kind=RUN_TASK, lane="r", agent_id=alpha, session_id=None, payload={})
    event = await _enqueue(container, "d", alpha)
    q = container.queue
    claimed = {(await q.claim([RUN_TASK])).id, (await q.claim([DELIVER_TASK])).id}
    assert claimed == {run.id, event.id}

    assert [t.id for t in await q.recover([DELIVER_TASK])] == [event.id]
    async with container.db.transaction() as s:
        assert (await s.get(Task, run.id)).status == TaskStatus.RUNNING


async def test_startup_recovers_running_tasks_before_any_worker_claims(container, make_agent):
    """启动恢复在启动阶段执行（Container.running 的 _prepare），领取循环里不恢复：否则抢先开始的请求或运行经
    run_now 当场领走的 task 会被复位，同一个 task 执行两次。"""
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    await container.queue.claim([DELIVER_TASK])

    running = asyncio.create_task(container.worker.run_forever())
    await asyncio.sleep(0.1)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    async with container.db.transaction() as s:
        assert (await s.get(Task, task.id)).status == TaskStatus.RUNNING

    await container._prepare()
    async with container.db.transaction() as s:
        assert (await s.get(Task, task.id)).status == TaskStatus.PENDING


async def test_every_claimable_task_runs_at_once_so_deliveries_never_wait_behind_long_runs(
    container, make_agent
):
    """worker 不设并发上限：可领取的 task 都马上执行（同一 lane 内仍串行）。曾经按 task 数设槽位，4 个长时间的会话
    运行占满槽位，79 个毫秒级的投递排队 17 分钟。稀缺的模型调用在模型层限流（agent_runtime.llm）。"""
    alpha = (await make_agent()).id
    lanes = [f"run-{i}" for i in range(6)]
    started: set[str] = set()
    all_started = asyncio.Event()
    delivered = asyncio.Event()

    class Blocking:
        async def execute(self, task: Task):  # noqa: ANN202
            started.add(task.lane)
            if started == set(lanes):
                all_started.set()
            await anyio.sleep_forever()

        async def failed_in(self, _s, _task: Task) -> None:  # noqa: ANN001
            pass

    class Recording:
        async def execute(self, _task: Task):  # noqa: ANN202
            delivered.set()
            return no_commit

        async def failed_in(self, _s, _task: Task) -> None:  # noqa: ANN001
            pass

    worker = Worker(container.queue, {RUN_TASK: Blocking(), DELIVER_TASK: Recording()}, container.bus)
    running = asyncio.create_task(worker.run_forever())
    try:
        async with container.db.transaction() as s:
            for lane in [*lanes, lanes[0]]:  # 同一 lane 的第二个排在第一个后面，不并发
                container.queue.enqueue(
                    s, kind=RUN_TASK, lane=lane, agent_id=alpha, session_id=None, payload={}
                )
        await asyncio.wait_for(all_started.wait(), 5)
        await _enqueue(container, "deliver-a", alpha)
        await asyncio.wait_for(delivered.wait(), 5)
        async with container.db.transaction() as s:
            pending = await s.scalars(
                select(Task).where(Task.kind == RUN_TASK, Task.status == TaskStatus.PENDING)
            )
            assert [task.lane for task in pending] == [lanes[0]]
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


def test_default_retry_policies_ride_out_a_model_outage_but_give_up_on_other_failures_quickly():
    """#144：relay 503 曾持续 9–30 分钟，原来所有暂时性失败共用 5 次（约 75 秒）就判失败，用户只能手动重试。
    按线上默认配置算：模型不可用的重试窗口约 30 分钟、单次间隔封顶 5 分钟；其余暂时性失败仍是 5 次约 75 秒。"""
    from muliao.config import RuntimeConfig
    from muliao.container import retries_of
    from muliao.platform.tasks import RetryPolicy

    retries = retries_of(RuntimeConfig())
    model, transient = retries.model, retries.transient

    def window(p: RetryPolicy) -> float:
        return sum(p.delay(n) for n in range(1, p.max_attempts))

    assert [model.delay(n) for n in range(1, 9)] == [5, 10, 20, 40, 80, 160, 300, 300]
    assert 25 * 60 <= window(model) <= 35 * 60
    assert window(transient) == 5 + 10 + 20 + 40


async def test_a_task_interrupted_between_the_two_limits_is_recovered_not_failed(container, make_agent):
    """尝试次数不记是哪类失败用掉的：启动恢复按较大的上限判断，正在按模型策略抗故障的运行不会因一次重启被判失败。"""
    alpha = (await make_agent()).id
    task = await _enqueue(container, "a", alpha)
    q = container.queue
    async with container.db.transaction() as s:
        row = await s.get(Task, task.id)
        row.status, row.attempts = TaskStatus.RUNNING, q.retries.transient.max_attempts
    assert q.retries.transient.max_attempts < q.retries.max_attempts

    await Worker(q, {DELIVER_TASK: object()}, container.bus).recover()  # type: ignore[dict-item]
    async with container.db.transaction() as s:
        assert (await s.get(Task, task.id)).status == TaskStatus.PENDING


async def test_model_unavailable_retries_past_the_ordinary_limit_up_to_its_own(container, make_agent):
    from muliao.errors import ModelUnavailable, TransientError

    alpha = (await make_agent()).id
    q = container.queue
    ordinary, model = q.retries.transient.max_attempts, q.retries.model.max_attempts
    assert model > ordinary

    async def exhaust(exc: Exception) -> Task:
        task = await _enqueue(container, f"lane-{type(exc).__name__}", alpha)
        while True:
            async with container.db.transaction() as s:
                (await s.get(Task, task.id)).not_before = utcnow() - timedelta(seconds=1)
            claimed = await q.claim_task(task.id, q.kinds)
            failed = await q.fail(claimed.id, str(exc), retry=q.retries.for_failure(exc), on_final=_nothing)
            if failed.status == TaskStatus.FAILED:
                return failed

    assert (await exhaust(TransientError("tool down"))).attempts == ordinary
    assert (await exhaust(ModelUnavailable("503 No available accounts"))).attempts == model


async def test_a_run_survives_a_model_outage_longer_than_the_ordinary_retry_limit(
    container, make_agent, models
):
    """模型连续返回 503 的次数超过一般暂时性失败的上限，运行照样在故障恢复后完成，不需要手动重试。"""
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.messages import ModelResponse, TextPart

    from muliao.sessions.api import Origin
    from tests.scripted import scripted
    from tests.test_flow import drain, start

    alpha = (await make_agent("alpha", model="m")).id
    outage = container.settings.runtime.model_retry_max_attempts - 1
    assert outage > container.settings.runtime.task_max_attempts
    calls = 0

    def flaky(_m, _i) -> ModelResponse:  # noqa: ANN001
        nonlocal calls
        calls += 1
        if calls <= outage:
            raise ModelHTTPError(status_code=503, model_name="m", body="No available accounts")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(flaky)
    await start(container, alpha, "hi", origin=Origin.TRIGGER)
    await drain(container)

    [turn] = [t for t in await container.queue.list(status=None, limit=20) if t.kind == RUN_TASK]
    assert turn.status == TaskStatus.DONE and turn.attempts == outage + 1
