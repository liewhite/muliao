"""领域事件与定向命令（platform.messaging）：与发送方的状态同事务提交、每个订阅者恰好处理一次、按发布顺序、契约违约
不重试、失败的投递可以按原顺序重试。"""

import anyio
import pytest
from sqlalchemy import select

from muliao.errors import TransientError
from muliao.platform.messaging import Command, Delivery, DomainEvent, Messaging
from muliao.platform.tables import EVENT_TASK, Task, TaskStatus
from muliao.platform.tasks import no_commit
from muliao.platform.worker import Worker
from tests.test_flow import idle


class Happened(DomainEvent):
    TYPE = "test.happened"
    n: int


class DoIt(Command):
    TYPE = "test.do_it"
    what: str


async def run_worker(container, messaging: Messaging) -> None:
    worker = Worker(container.queue, {EVENT_TASK: messaging}, container.bus)
    async with anyio.create_task_group() as group:
        group.start_soon(worker.run_forever)
        await idle(container)
        group.cancel_scope.cancel()


async def deliveries(container) -> list[Task]:
    async with container.db.transaction() as s:
        return list((await s.scalars(select(Task).where(Task.kind == EVENT_TASK).order_by(Task.id))).all())


async def test_each_subscriber_handles_a_committed_event_once_with_the_same_id(container):
    messaging = Messaging(container.queue)
    seen: dict[str, list[tuple[int, str]]] = {"a": [], "b": []}

    def recorder(name: str):
        async def handle(event: Happened, delivery: Delivery):
            seen[name].append((event.n, delivery.message_id))
            return no_commit

        return handle

    messaging.subscribe(Happened, "a", recorder("a"))
    messaging.subscribe(Happened, "b", recorder("b"))
    async with container.db.transaction() as s:
        messaging.publish_in(s, Happened(n=1))
    await run_worker(container, messaging)

    [(n, event_id)] = seen["a"]
    assert n == 1 and seen["b"] == [(1, event_id)]
    assert [t.status for t in await deliveries(container)] == [TaskStatus.DONE, TaskStatus.DONE]


async def test_an_event_published_in_a_rolled_back_transaction_is_never_delivered(container):
    messaging = Messaging(container.queue)
    messaging.subscribe(Happened, "a", lambda _e, _d: pytest.fail("delivered"))

    with pytest.raises(RuntimeError, match="boom"):
        async with container.db.transaction() as s:
            messaging.publish_in(s, Happened(n=1))
            raise RuntimeError("boom")

    assert await deliveries(container) == []


async def test_a_subscriber_sees_events_in_publish_order(container):
    messaging = Messaging(container.queue)
    order: list[int] = []

    async def handle(event: Happened, _d: Delivery):
        # 前一条慢一点：同一条 lane 上后一条仍然等它
        await anyio.sleep(0.05 if event.n == 1 else 0)
        order.append(event.n)
        return no_commit

    messaging.subscribe(Happened, "a", handle)
    for n in (1, 2, 3):
        async with container.db.transaction() as s:
            messaging.publish_in(s, Happened(n=n))
    await run_worker(container, messaging)

    assert order == [1, 2, 3]


async def test_a_handlers_commit_is_atomic_with_the_delivery_and_a_retry_redelivers_the_same_id(container):
    messaging = Messaging(container.queue)
    attempts: list[str] = []
    committed: list[str] = []

    async def handle(_event: Happened, delivery: Delivery):
        attempts.append(delivery.message_id)

        async def commit(_s):
            if len(attempts) == 1:
                raise TransientError("database briefly unavailable")
            committed.append(delivery.message_id)

        return commit

    messaging.subscribe(Happened, "a", handle)
    async with container.db.transaction() as s:
        messaging.publish_in(s, Happened(n=1))
    await run_worker(container, messaging)

    assert attempts == [attempts[0]] * 2 and committed == [attempts[0]]


async def test_a_command_goes_to_its_one_handler_and_needs_one(container):
    messaging = Messaging(container.queue)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    async with container.db.transaction() as s:
        with pytest.raises(RuntimeError, match="no handler"):
            messaging.send_in(s, DoIt(what="x"))
    messaging.handle(DoIt, "doer", handle)
    with pytest.raises(RuntimeError, match="already has a handler"):
        messaging.handle(DoIt, "other", handle)
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="x"))
    await run_worker(container, messaging)

    assert done == ["x"]


async def test_a_delivery_this_version_cannot_read_fails_at_once_and_can_be_retried_in_place(container):
    messaging = Messaging(container.queue)
    handled: list[int] = []

    async def handle(event: Happened, _d: Delivery):
        handled.append(event.n)
        return no_commit

    messaging.subscribe(Happened, "a", handle)
    async with container.db.transaction() as s:
        messaging.publish_in(s, Happened(n=1))
        messaging.publish_in(s, Happened(n=2))
    async with container.db.transaction() as s:
        first = await s.scalar(select(Task).where(Task.kind == EVENT_TASK).order_by(Task.id).limit(1))
        first.payload = {**first.payload, "data": {"n": "not a number"}}
    await run_worker(container, messaging)

    broken, ok = await deliveries(container)
    assert (broken.status, broken.attempts) == (TaskStatus.FAILED, 1)
    assert "invalid test.happened" in broken.error and ok.status == TaskStatus.DONE

    # 修好数据后人工重试：订阅 lane 上还有别的投递时也允许，沿用原 id
    async with container.db.transaction() as s:
        row = await s.get(Task, broken.id)
        row.payload = {**row.payload, "data": {"n": 1}}
    async with container.db.transaction() as s:
        messaging.publish_in(s, Happened(n=3))
    await container.queue.retry(broken.id)
    await run_worker(container, messaging)

    assert handled == [2, 1, 3]


def _now_worker(container, messaging: Messaging) -> Worker:
    return Worker(container.queue, {EVENT_TASK: messaging}, container.bus)


async def test_run_now_executes_a_committed_command_at_once(container):
    messaging = Messaging(container.queue)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle)
    async with container.db.transaction() as s:
        task = messaging.send_in(s, DoIt(what="x"))
    await _now_worker(container, messaging).run_now(task.id)

    assert done == ["x"]
    assert [t.status for t in await deliveries(container)] == [TaskStatus.DONE]


async def test_the_run_now_port_runs_through_the_bound_worker_and_refuses_before_binding(container):
    """各领域拿到的是 RunNow 端口，worker 在装配的最后才绑定：绑定之前调用、重复绑定都是装配的 bug，当场报错。"""
    from muliao.platform.worker import RunNow

    messaging = Messaging(container.queue)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle)
    async with container.db.transaction() as s:
        task = messaging.send_in(s, DoIt(what="x"))
    run_now = RunNow()
    with pytest.raises(RuntimeError, match="before a worker was bound"):
        await run_now(task.id)
    worker = _now_worker(container, messaging)
    run_now.bind(worker)
    with pytest.raises(RuntimeError, match="already bound"):
        run_now.bind(worker)
    await run_now(task.id)

    assert done == ["x"]


async def test_run_now_leaves_a_task_behind_an_earlier_one_on_its_lane_to_the_worker(container):
    messaging = Messaging(container.queue)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle, lane=lambda _c: "one-lane")
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="first"))
        second = messaging.send_in(s, DoIt(what="second"))
    await _now_worker(container, messaging).run_now(second.id)
    assert done == []

    await run_worker(container, messaging)
    assert done == ["first", "second"]


async def test_run_now_does_nothing_for_a_task_the_worker_already_claimed(container):
    messaging = Messaging(container.queue)
    messaging.handle(DoIt, "do", lambda _c, _d: pytest.fail("executed twice"))
    async with container.db.transaction() as s:
        task = messaging.send_in(s, DoIt(what="x"))
    assert await container.queue.claim([EVENT_TASK]) is not None

    await _now_worker(container, messaging).run_now(task.id)


async def test_a_cancelled_run_now_hands_the_task_back_and_its_lane_moves_on(container):
    messaging = Messaging(container.queue)
    started, done = anyio.Event(), []

    async def handle(command: DoIt, _d: Delivery):
        if command.what == "slow" and not done:
            started.set()
            await anyio.sleep(30)
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle, lane=lambda _c: "one-lane")
    async with container.db.transaction() as s:
        slow = messaging.send_in(s, DoIt(what="slow"))
        messaging.send_in(s, DoIt(what="after"))
    async with anyio.create_task_group() as group:
        group.start_soon(_now_worker(container, messaging).run_now, slow.id)
        await started.wait()
        # 调用方被取消（请求断开、调用它的运行被停止）
        group.cancel_scope.cancel()
    async with container.db.transaction() as s:
        assert (await s.get(Task, slow.id)).status == TaskStatus.PENDING

    done.append("marker")  # 之后由 worker 重做时不再慢
    await run_worker(container, messaging)
    assert done == ["marker", "slow", "after"]


async def test_a_failing_run_now_is_recorded_like_any_task(container):
    messaging = Messaging(container.queue)

    async def handle(_c: DoIt, _d: Delivery):
        raise TransientError("try later")

    messaging.handle(DoIt, "do", handle)
    async with container.db.transaction() as s:
        task = messaging.send_in(s, DoIt(what="x"))
    await _now_worker(container, messaging).run_now(task.id)

    [row] = await deliveries(container)
    assert row.status == TaskStatus.PENDING and row.attempts == 1 and "try later" in (row.error or "")
