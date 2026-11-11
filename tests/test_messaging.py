"""领域事件与定向命令（platform.messaging）：与发送方的状态同事务提交、每个订阅者恰好处理一次、按发布顺序、契约违约
不重试、失败的投递可以按原顺序重试。"""

import anyio
import pytest
from sqlalchemy import select

from muliao.errors import TransientError
from muliao.platform.messaging import Command, Delivery, DomainEvent, Messaging
from muliao.platform.tables import EVENT_TASK, Task, TaskStatus
from muliao.platform.tasks import no_commit
from muliao.platform.worker import RunNow, Worker
from tests.test_flow import idle


class Happened(DomainEvent):
    TYPE = "test.happened"
    n: int


class DoIt(Command):
    TYPE = "test.do_it"
    what: str


async def _left_to_the_worker(_task_id: int) -> None:
    """不马上执行：投递都留给 worker（测 worker 的执行规则）。"""


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
    messaging = Messaging(container.queue, _left_to_the_worker)
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
    messaging = Messaging(container.queue, _left_to_the_worker)
    messaging.subscribe(Happened, "a", lambda _e, _d: pytest.fail("delivered"))

    with pytest.raises(RuntimeError, match="boom"):
        async with container.db.transaction() as s:
            messaging.publish_in(s, Happened(n=1))
            raise RuntimeError("boom")

    assert await deliveries(container) == []


async def test_a_subscriber_sees_events_in_publish_order(container):
    messaging = Messaging(container.queue, _left_to_the_worker)
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
    messaging = Messaging(container.queue, _left_to_the_worker)
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
    messaging = Messaging(container.queue, _left_to_the_worker)
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
    messaging = Messaging(container.queue, _left_to_the_worker)
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
    messaging = Messaging(container.queue, _left_to_the_worker)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle)
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="x"))
    [task] = await deliveries(container)
    await _now_worker(container, messaging).run_now(task.id)

    assert done == ["x"]
    assert [t.status for t in await deliveries(container)] == [TaskStatus.DONE]


async def test_the_run_now_port_runs_through_the_bound_worker_and_refuses_before_binding(container):
    """各领域拿到的是 RunNow 端口，worker 在装配的最后才绑定：绑定之前调用、重复绑定都是装配的 bug，当场报错。"""
    messaging = Messaging(container.queue, _left_to_the_worker)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle)
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="x"))
    [task] = await deliveries(container)
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
    messaging = Messaging(container.queue, _left_to_the_worker)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle, lane=lambda _c: "one-lane")
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="first"))
        messaging.send_in(s, DoIt(what="second"))
    _first, second = await deliveries(container)
    await _now_worker(container, messaging).run_now(second.id)
    assert done == []

    await run_worker(container, messaging)
    assert done == ["first", "second"]


async def test_run_now_does_nothing_for_a_task_the_worker_already_claimed(container):
    messaging = Messaging(container.queue, _left_to_the_worker)
    messaging.handle(DoIt, "do", lambda _c, _d: pytest.fail("executed twice"))
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="x"))
    [task] = await deliveries(container)
    assert await container.queue.claim([EVENT_TASK]) is not None

    await _now_worker(container, messaging).run_now(task.id)


async def test_a_cancelled_run_now_hands_the_task_back_and_its_lane_moves_on(container):
    messaging = Messaging(container.queue, _left_to_the_worker)
    started, done = anyio.Event(), []

    async def handle(command: DoIt, _d: Delivery):
        if command.what == "slow" and not done:
            started.set()
            await anyio.sleep(30)
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle, lane=lambda _c: "one-lane")
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="slow"))
        messaging.send_in(s, DoIt(what="after"))
    slow, _after = await deliveries(container)
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
    messaging = Messaging(container.queue, _left_to_the_worker)

    async def handle(_c: DoIt, _d: Delivery):
        raise TransientError("try later")

    messaging.handle(DoIt, "do", handle)
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="x"))
    [task] = await deliveries(container)
    await _now_worker(container, messaging).run_now(task.id)

    [row] = await deliveries(container)
    assert row.status == TaskStatus.PENDING and row.attempts == 1 and "try later" in (row.error or "")


def _assembled(container) -> Messaging:
    """像组合根一样装配：马上执行接到处理这些投递的 worker 上。"""
    run_now = RunNow()
    messaging = Messaging(container.queue, run_now)
    run_now.bind(_now_worker(container, messaging))
    return messaging


async def test_committed_deliveries_run_before_the_senders_transaction_returns(container):
    """发送方不收集投递：事务提交后（事务锁已释放，处理者可以开自己的事务）按发送顺序马上执行，回滚的不执行。"""
    messaging = _assembled(container)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        async with container.db.transaction():
            pass
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle)
    with pytest.raises(RuntimeError, match="boom"):
        async with container.db.transaction() as s:
            messaging.send_in(s, DoIt(what="rolled back"))
            raise RuntimeError("boom")
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="a"))
        messaging.send_in(s, DoIt(what="b"))
        assert done == []
    assert done == ["a", "b"]
    assert [t.status for t in await deliveries(container)] == [TaskStatus.DONE] * 2


async def test_deliveries_sent_from_a_handlers_commit_also_run_at_once(container):
    """订阅方在 Commit 里发出的投递（与投递完成同一事务）同样在提交后马上执行，不必等 worker。"""
    messaging = _assembled(container)
    done: list[str] = []

    async def first(_command: DoIt, _d: Delivery):
        async def commit(s):
            messaging.publish_in(s, Happened(n=2))

        return commit

    async def then(event: Happened, _d: Delivery):
        done.append(f"event {event.n}")
        return no_commit

    messaging.handle(DoIt, "do", first)
    messaging.subscribe(Happened, "then", then)
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="x"))
    assert done == ["event 2"]
    assert [t.status for t in await deliveries(container)] == [TaskStatus.DONE] * 2


async def test_a_delivery_that_cannot_be_claimed_at_once_does_not_fail_the_sender_and_the_worker_runs_it(
    container, monkeypatch
):
    """领取出错（数据库暂时不可用）只是没能马上执行：发送方的写入已经提交，不报错，由 worker 执行。"""
    messaging = _assembled(container)
    done: list[str] = []

    async def unavailable(*_args):  # noqa: ANN202
        raise TransientError("database briefly unavailable")

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle)
    with monkeypatch.context() as m:
        m.setattr(container.queue, "claim_task", unavailable)
        async with container.db.transaction() as s:
            messaging.send_in(s, DoIt(what="x"))
    assert done == []

    await run_worker(container, messaging)
    assert done == ["x"]


async def test_a_slow_receiver_is_left_to_the_worker(container):
    """处理要等别的工作结束的接收方（slow）不在发送方的任务里执行：发送方不等它。"""
    messaging = _assembled(container)
    done: list[str] = []

    async def handle(command: DoIt, _d: Delivery):
        done.append(command.what)
        return no_commit

    messaging.handle(DoIt, "do", handle, slow=True)
    async with container.db.transaction() as s:
        messaging.send_in(s, DoIt(what="x"))
    assert done == []

    await run_worker(container, messaging)
    assert done == ["x"]


async def test_assembly_refuses_a_command_without_handler_or_an_event_without_subscriber(container):
    """漏登记（如漏放进组合根列表的领域）在装配时报错（Messaging.check_wired），不等到发送时或静默不投；测试里定义的
    消息不算。组合根装配好的 messaging 已经过了这道检查。"""
    container.messaging.check_wired()
    bare = Messaging(container.queue, _left_to_the_worker)
    with pytest.raises(RuntimeError) as e:
        bare.check_wired()
    assert "session.run_ended" in str(e.value) and "issues.post_event" in str(e.value)
    assert "test.happened" not in str(e.value)


async def test_the_service_runs_the_loops_the_domains_declare(container):
    """常驻循环由各领域声明（platform.wiring.Parts.loops），组合根只遍历：服务启动的就是这些。"""
    assert sorted(name for name, _ in container.loops) == sorted(
        ["worker", "checks", "retention", "due", "streams", "mail", "assets"]
    )
