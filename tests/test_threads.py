"""会话领域：delegate_task / ask_agent / wait_for、thread 续聊、挂起与唤醒、失败与删除时的结果路由。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from muliao.errors import Conflict
from muliao.models import Message, Session, Task, TaskKind, TaskStatus, Wait
from tests.scripted import scripted
from tests.test_flow import drain, last_user_prompt, start, tasks_of, tool_returns


def steps(*responses: Callable[[list[ModelMessage]], ModelResponse]):
    """第 n 次模型调用（按历史里已有的模型响应数计）返回第 n 个响应。"""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return responses[sum(isinstance(m, ModelResponse) for m in messages)](messages)

    return scripted(fn)


def call(tool: str, args: dict, call_id: str = "c1"):
    return lambda _m: ModelResponse(parts=[ToolCallPart(tool, args, tool_call_id=call_id)])


def answer_with_tool_results(prefix: str):
    def fn(messages: list[ModelMessage]) -> ModelResponse:
        results = sorted(str(r.content) for r in tool_returns(messages))
        return ModelResponse(parts=[TextPart(f"{prefix}: {' | '.join(results)}")])

    return fn


def echo(prefix: str, seen: list[int] | None = None):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(len(messages))
        return ModelResponse(parts=[TextPart(f"{prefix} {last_user_prompt(messages)}")])

    return scripted(fn)


def gated(gate: asyncio.Event, text: str) -> FunctionModel:
    """在 gate 打开前一直运行中的模型。"""

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        await gate.wait()
        yield text

    return FunctionModel(stream_function=stream)


async def eventually[T](probe: Callable[[], Awaitable[T]]) -> T:
    """轮询数据库状态直到满足（任务状态没有可订阅的事件）。"""
    async with asyncio.timeout(5):
        while True:
            if result := await probe():
                return result
            await asyncio.sleep(0.02)


async def turn_in(container, session_id: str, status: TaskStatus) -> Task | None:
    """session 里处于给定状态的那一轮。"""
    async with container.db.transaction() as s:
        return await s.scalar(
            select(Task).where(
                Task.session_id == session_id, Task.kind == TaskKind.TURN, Task.status == status
            )
        )


async def threads_of(container, parent_id: str) -> list[Session]:
    return await container.sessions.threads(parent_id)


async def last_answer(container, session_id: str) -> str:
    history = await container.sessions.history(session_id)
    assert isinstance(history[-1], ModelResponse)
    return history[-1].parts[0].content


async def test_ask_agent_waits_for_answer_and_follow_up_keeps_thread_context(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("reviewer", model="v")
    child_seen: list[int] = []
    models.models["v"] = echo("reviewed", child_seen)
    ask = {"agent": "reviewer", "thread": "review"}
    models.models["l"] = steps(
        call("ask_agent", {**ask, "message": "v1"}),
        answer_with_tool_results("verdict"),
        call("ask_agent", {**ask, "message": "v2"}),
        answer_with_tool_results("verdict"),
    )

    parent = await start(container, lead, "write code")
    await drain(container)
    assert await last_answer(container, parent.id) == "verdict: reviewed v1"

    await container.sessions.reply(parent.id, "fix it")
    await drain(container)
    assert await last_answer(container, parent.id) == "verdict: reviewed v2"

    [child] = await threads_of(container, parent.id)
    assert child.private and child.thread == "review"
    # 第二次 ask 时对方看得到第一次的上下文
    assert child_seen == [1, 3]
    # 子会话的回答交给父 agent，不推送；父会话每轮推送一次最终回答
    deliveries = await tasks_of(container, TaskKind.DELIVER)
    assert [d.session_id for d in deliveries] == [parent.id, parent.id]
    assert [d.payload["body"] for d in deliveries] == ["verdict: reviewed v1", "verdict: reviewed v2"]
    # 一轮的消息记在 root turn 名下：父会话两轮，各自完整
    async with container.db.transaction() as s:
        task_ids = (await s.scalars(select(Message.task_id).where(Message.session_id == parent.id))).all()
    turns = [t.id for t in await tasks_of(container, TaskKind.TURN) if t.session_id == parent.id]
    assert sorted(set(task_ids)) == turns


async def test_parallel_asks_resume_with_all_answers(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("a", model="a")
    await make_agent("b", model="b")
    models.models["a"] = echo("A")
    models.models["b"] = echo("B")
    models.models["l"] = steps(
        lambda _m: ModelResponse(
            parts=[
                ToolCallPart("ask_agent", {"agent": "a", "message": "x", "thread": "ta"}, tool_call_id="c1"),
                ToolCallPart("ask_agent", {"agent": "b", "message": "y", "thread": "tb"}, tool_call_id="c2"),
            ]
        ),
        answer_with_tool_results("both"),
    )

    parent = await start(container, lead, "go")
    await drain(container)

    assert await last_answer(container, parent.id) == "both: A x | B y"
    # 恢复时用过的等待记录随结果进入历史后清除
    async with container.db.transaction() as s:
        assert (await s.scalars(select(Wait))).all() == []


async def test_delegate_then_wait_for_running_thread_suspends_until_it_finishes(
    container, make_agent, settings, models
):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    gate = asyncio.Event()
    models.models["w"] = gated(gate, "report ready")
    models.models["l"] = steps(
        call("delegate_task", {"agent": "worker", "task": "write report", "thread": "report"}),
        call("wait_for", {"thread": "report"}, "c2"),
        answer_with_tool_results("got"),
    )

    parent = await start(container, lead, "need report")
    drained = asyncio.create_task(drain(container))
    await eventually(lambda: turn_in(container, parent.id, TaskStatus.WAITING))
    gate.set()
    await drained

    assert await last_answer(container, parent.id) == "got: report ready"
    [child] = await threads_of(container, parent.id)
    assert not child.private
    # 公开 thread 的回答推送给用户，父会话的最终回答也推送
    deliveries = await tasks_of(container, TaskKind.DELIVER)
    assert sorted(d.session_id for d in deliveries) == sorted([parent.id, child.id])


async def test_wait_for_finished_thread_returns_immediately(container, make_agent, models, caplog):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    models.models["w"] = echo("did")
    models.models["l"] = steps(
        call("delegate_task", {"agent": "worker", "task": "job", "thread": "t"}),
        lambda _m: ModelResponse(parts=[TextPart("delegated")]),
        call("wait_for", {"thread": "t"}, "c2"),
        answer_with_tool_results("result"),
    )

    parent = await start(container, lead, "start")
    await drain(container)
    await container.sessions.reply(parent.id, "and?")
    await drain(container)

    assert await last_answer(container, parent.id) == "result: did job"
    assert "turn suspended" not in caplog.messages


async def test_failed_child_turn_is_returned_as_failed_result(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("broken", model="x")

    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="x", body="bad request")

    models.models["x"] = scripted(fail)
    models.models["l"] = steps(
        call("ask_agent", {"agent": "broken", "message": "do", "thread": "t"}),
        answer_with_tool_results("outcome"),
    )

    parent = await start(container, lead, "go")
    await drain(container)

    answer = await last_answer(container, parent.id)
    assert answer.startswith("outcome: [failed] ModelHTTPError") and "400" in answer


async def test_deleting_awaited_thread_wakes_waiter_with_error(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("slow", model="s")
    gate = asyncio.Event()
    models.models["s"] = gated(gate, "too late")
    models.models["l"] = steps(
        call("ask_agent", {"agent": "slow", "message": "do", "thread": "t"}),
        answer_with_tool_results("outcome"),
    )
    parent = await start(container, lead, "go")

    worker = asyncio.create_task(container.worker.run_forever())

    async def child_running() -> list[Task]:
        turns = await tasks_of(container, TaskKind.TURN)
        return [t for t in turns if t.status == TaskStatus.RUNNING and t.session_id != parent.id]

    await eventually(child_running)
    [child] = await threads_of(container, parent.id)
    with pytest.raises(Conflict):
        await container.sessions.delete(child.id)
    # 模拟进程在子任务运行中重启：子任务回到 pending，此时可以删除
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    await container.queue.recover()
    await container.sessions.delete(child.id)
    gate.set()
    await drain(container)

    assert (await last_answer(container, parent.id)).startswith(
        f"outcome: [failed] session {child.id} was deleted"
    )


async def test_deleting_parent_deletes_private_threads_and_keeps_public_ones(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    models.models["l"] = steps(
        lambda _m: ModelResponse(
            parts=[
                ToolCallPart(
                    "delegate_task", {"agent": "worker", "task": "a", "thread": "pub"}, tool_call_id="c1"
                ),
                ToolCallPart(
                    "ask_agent", {"agent": "worker", "message": "b", "thread": "priv"}, tool_call_id="c2"
                ),
            ]
        ),
        answer_with_tool_results("done"),
    )
    parent = await start(container, lead, "go")
    await drain(container)
    pub, priv = sorted(await threads_of(container, parent.id), key=lambda t: t.private)

    await container.sessions.delete(parent.id)

    async with container.db.transaction() as s:
        assert await s.get(Session, priv.id) is None
        kept = await s.get(Session, pub.id)
        assert kept is not None and kept.parent_session_id is None


async def test_private_thread_is_read_only_for_user_and_hidden_from_list(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    models.models["l"] = steps(
        call("ask_agent", {"agent": "worker", "message": "q", "thread": "t"}),
        answer_with_tool_results("a"),
    )
    parent = await start(container, lead, "go")
    await drain(container)
    [child] = await threads_of(container, parent.id)

    with pytest.raises(Conflict):
        await container.sessions.reply(child.id, "hi")
    listed = await container.sessions.list(agent_id=None, origins=None, limit=50, offset=0)
    assert [x.id for x in listed] == [parent.id]
    async with container.db.transaction() as s:
        assert not (await s.get(Session, child.id)).unread


async def test_thread_ownership_rules_are_reported_to_model(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    await make_agent("other", model="o")
    errors: list[str] = []
    script = [
        ("ask_agent", {"agent": "worker", "message": "q", "thread": "t"}),
        ("delegate_task", {"agent": "worker", "task": "q", "thread": "t"}),
        ("ask_agent", {"agent": "other", "message": "q", "thread": "t"}),
        ("wait_for", {"thread": "nope"}),
    ]

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ModelRequest, RetryPromptPart

        last = messages[-1]
        if isinstance(last, ModelRequest):
            errors.extend(str(p.content) for p in last.parts if isinstance(p, RetryPromptPart))
        step = sum(isinstance(m, ModelResponse) for m in messages)
        if step < len(script):
            name, args = script[step]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{step}")])
        return ModelResponse(parts=[TextPart("end")])

    models.models["l"] = scripted(fn)
    await start(container, lead, "go")
    await drain(container)

    assert len(errors) == 3
    assert "private to this conversation" in errors[0]
    assert "belongs to another agent" in errors[1]
    assert "no thread named 'nope'" in errors[2]


async def test_waiting_turn_blocks_its_lane_until_resumed(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("slow", model="s")
    gate = asyncio.Event()
    models.models["s"] = gated(gate, "slow answer")
    models.models["l"] = steps(
        call("ask_agent", {"agent": "slow", "message": "do", "thread": "t"}),
        answer_with_tool_results("first"),
        lambda m: ModelResponse(parts=[TextPart(f"second {last_user_prompt(m)}")]),
    )
    parent = await start(container, lead, "go")
    drained = asyncio.create_task(drain(container))
    await eventually(lambda: turn_in(container, parent.id, TaskStatus.WAITING))
    followup = await container.sessions.reply(parent.id, "more")
    await asyncio.sleep(0.2)
    async with container.db.transaction() as s:
        assert (await s.get(Task, followup.id)).status == TaskStatus.PENDING
    gate.set()
    await drained

    history = await container.sessions.history(parent.id)
    texts = [
        m.parts[0].content
        for m in history
        if isinstance(m, ModelResponse) and isinstance(m.parts[0], TextPart)
    ]
    assert texts == ["first: slow answer", "second more"]


async def test_failed_suspended_turn_rolls_back_and_retry_reruns_whole_turn(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    models.models["w"] = echo("did")
    failures = {"left": 1}

    def answer_or_fail(messages: list[ModelMessage]) -> ModelResponse:
        if failures["left"]:
            failures["left"] -= 1
            raise ModelHTTPError(status_code=400, model_name="l", body="bad request")
        return answer_with_tool_results("final")(messages)

    models.models["l"] = steps(
        call("ask_agent", {"agent": "worker", "message": "q", "thread": "t"}), answer_or_fail
    )
    parent = await start(container, lead, "go")
    await drain(container)

    # 一轮是原子的：挂起前提交的工具调用随失败一起撤销，历史里不留半截
    assert await container.sessions.history(parent.id) == []
    failed = await turn_in(container, parent.id, TaskStatus.FAILED)
    assert failed is not None and "400" in failed.error

    await container.queue.retry(failed.id)
    await drain(container)

    assert await last_answer(container, parent.id) == "final: did q"
    [child] = await threads_of(container, parent.id)
    assert len([t for t in await tasks_of(container, TaskKind.TURN) if t.session_id == child.id]) == 2


async def test_wait_for_sees_result_of_manually_retried_thread(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    failures = {"left": 1}

    def flaky(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if failures["left"]:
            failures["left"] -= 1
            raise ModelHTTPError(status_code=400, model_name="w", body="bad request")
        return ModelResponse(parts=[TextPart("second try worked")])

    models.models["w"] = scripted(flaky)
    models.models["l"] = steps(
        call("delegate_task", {"agent": "worker", "task": "job", "thread": "t"}),
        lambda _m: ModelResponse(parts=[TextPart("delegated")]),
        call("wait_for", {"thread": "t"}, "c2"),
        answer_with_tool_results("result"),
    )
    parent = await start(container, lead, "start")
    await drain(container)
    [child] = await threads_of(container, parent.id)
    await container.queue.retry((await turn_in(container, child.id, TaskStatus.FAILED)).id)
    await drain(container)
    await container.sessions.reply(parent.id, "and?")
    await drain(container)

    assert await last_answer(container, parent.id) == "result: second try worked"


async def test_waiting_on_a_suspended_thread_gets_its_final_answer(container, make_agent, models):
    """所等的那一轮自己也挂起了（嵌套 ask）：要等到它给出最终回答，而不是拿到中间的那段话。"""
    lead = (await make_agent("lead", model="l")).id
    await make_agent("mid", model="m")
    await make_agent("leaf", model="f")
    gate = asyncio.Event()
    models.models["f"] = gated(gate, "leaf fact")
    models.models["m"] = steps(
        lambda _m: ModelResponse(
            parts=[
                TextPart("let me ask leaf first"),
                ToolCallPart(
                    "ask_agent", {"agent": "leaf", "message": "fact?", "thread": "leaf"}, tool_call_id="m1"
                ),
            ]
        ),
        answer_with_tool_results("mid says"),
    )
    models.models["l"] = steps(
        call("delegate_task", {"agent": "mid", "task": "research", "thread": "m"}),
        lambda _m: ModelResponse(parts=[TextPart("delegated")]),
        call("wait_for", {"thread": "m"}, "c2"),
        answer_with_tool_results("lead got"),
    )
    parent = await start(container, lead, "go")
    drained = asyncio.create_task(drain(container))

    async def mid_waiting() -> Task | None:
        threads = await threads_of(container, parent.id)
        return threads and await turn_in(container, threads[0].id, TaskStatus.WAITING)

    await eventually(mid_waiting)
    # 第一轮结束后追问：wait_for 等的正是那个已挂起的 thread
    await container.sessions.reply(parent.id, "result?")
    await eventually(lambda: turn_in(container, parent.id, TaskStatus.WAITING))
    gate.set()
    await drained

    assert await last_answer(container, parent.id) == "lead got: mid says: leaf fact"


async def test_deleting_awaited_agent_wakes_waiter_with_error(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    slow = (await make_agent("slow", model="s")).id
    models.models["l"] = steps(
        call("ask_agent", {"agent": "slow", "message": "do", "thread": "t"}),
        answer_with_tool_results("outcome"),
    )
    parent = await start(container, lead, "go")
    # 只执行父 agent 这一轮：挂起后子任务还在排队时删除目标 agent
    worker = asyncio.create_task(container.worker.run_forever())
    await eventually(lambda: turn_in(container, parent.id, TaskStatus.WAITING))
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
    await container.queue.recover()
    [child] = await threads_of(container, parent.id)

    await container.agents.delete(slow)
    await drain(container)

    assert (await last_answer(container, parent.id)).startswith(
        f"outcome: [failed] session {child.id} was deleted"
    )


async def test_parallel_calls_competing_for_one_thread_name_are_corrected_by_model(
    container, make_agent, models
):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("a", model="a")
    await make_agent("b", model="b")
    models.models["a"] = echo("A")
    retries: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ModelRequest, RetryPromptPart

        last = messages[-1]
        if isinstance(last, ModelRequest):
            retries.extend(str(p.content) for p in last.parts if isinstance(p, RetryPromptPart))
        if sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "ask_agent", {"agent": "a", "message": "x", "thread": "same"}, tool_call_id="c1"
                    ),
                    ToolCallPart(
                        "ask_agent", {"agent": "b", "message": "y", "thread": "same"}, tool_call_id="c2"
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart("ok")])

    models.models["l"] = scripted(fn)
    parent = await start(container, lead, "go")
    await drain(container)

    assert any("belongs to another agent" in r for r in retries)
    assert await turn_in(container, parent.id, TaskStatus.DONE) is not None
