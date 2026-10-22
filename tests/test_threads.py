"""会话领域：delegate_task（公开 thread）、ask_agent（私有 thread，发问方停在这次调用上，对方的结局作为它的结果
交回）、thread 续聊、失败与删除的语义、续写规则。"""

import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.errors import Conflict, Forbidden
from muliao.platform.tables import TaskStatus
from muliao.sessions.api import Origin
from muliao.sessions.inbound import Inbound, NewSession
from muliao.sessions.kinds import SessionKind
from muliao.sessions.lanes import RUN_TASK
from muliao.sessions.mailbox import queued_in
from muliao.sessions.tables import Message, Session
from muliao.sessions.threads import threads_of as sessions_threads_of
from tests.scripted import scripted
from tests.test_flow import drain, history_of, idle, last_user_prompt, reply, start, tasks_of, tool_returns


def steps(*responses):
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


async def threads_of(container, parent_id: str) -> list[Session]:
    async with container.db.transaction() as s:
        return await sessions_threads_of(s, parent_id)


async def last_answer(container, session_id: str) -> str:
    history = await history_of(container, session_id)
    assert isinstance(history[-1], ModelResponse)
    return history[-1].parts[0].content


async def turns_in(container, session_id: str):
    return [t for t in await tasks_of(container, RUN_TASK) if t.session_id == session_id]


async def test_ask_agent_returns_answer_and_follow_up_keeps_thread_context(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("reviewer", model="v")
    child_seen: list[int] = []
    models.models["v"] = echo("reviewed", child_seen)
    ask = {"agent": "reviewer", "thread": "review"}
    models.models["l"] = steps(
        call("ask_agent", {**ask, "message": "v1"}, "c1"),
        answer_with_tool_results("verdict"),
        call("ask_agent", {**ask, "message": "v2"}, "c2"),
        answer_with_tool_results("verdict"),
    )

    parent = await start(container, lead, "write code")
    await drain(container)
    assert await last_answer(container, parent.id) == "verdict: reviewed v1"
    await reply(container, parent.id, "fix it")
    await drain(container)
    assert await last_answer(container, parent.id) == "verdict: reviewed v2"

    [child] = await threads_of(container, parent.id)
    assert child.kind == SessionKind.PRIVATE_THREAD and child.thread == "review"
    # 第二次 ask 时对方看得到第一次的上下文
    assert child_seen == [1, 3]
    # 私有 thread 和其他会话一样由自己的运行处理，消息记在运行名下
    child_turns = await turns_in(container, child.id)
    assert [t.status for t in child_turns] == [TaskStatus.DONE, TaskStatus.DONE]
    async with container.db.transaction() as s:
        task_ids = (await s.scalars(select(Message.task_id).where(Message.session_id == child.id))).all()
    assert sorted(set(task_ids)) == [t.id for t in child_turns]
    # 发问方停在 ask_agent 上时不推送；子会话的回答交给父 agent，不推送；父会话做完时推送最终回答
    notes = await container.notifications.list(100)
    assert [n.session_id for n in notes] == [parent.id, parent.id]
    # 发问方等待时不占运行：每次 ask 前后各是一次运行
    assert [t.status for t in await turns_in(container, parent.id)] == [TaskStatus.DONE] * 4


async def test_parallel_asks_run_concurrently_and_asks_to_one_thread_are_answered_together(
    container, make_agent, models
):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("a", model="a")
    await make_agent("b", model="b")
    a_seen: list[int] = []
    models.models["a"] = echo("A", a_seen)
    models.models["b"] = echo("B")
    models.models["l"] = steps(
        lambda _m: ModelResponse(
            parts=[
                ToolCallPart("ask_agent", {"agent": "a", "message": "x", "thread": "ta"}, tool_call_id="c1"),
                ToolCallPart("ask_agent", {"agent": "a", "message": "y", "thread": "ta"}, tool_call_id="c2"),
                ToolCallPart("ask_agent", {"agent": "b", "message": "z", "thread": "tb"}, tool_call_id="c3"),
            ]
        ),
        answer_with_tool_results("all"),
    )

    parent = await start(container, lead, "go")
    await drain(container)

    # 同一个私有 thread 的两次 ask 进了同一个信箱：对方一次看到两个问题、一起回答，两次调用拿到同一个回答
    assert await last_answer(container, parent.id) == "all: A x\ny | A x\ny | B z"
    assert a_seen == [1]


async def test_nested_ask_returns_the_final_answer_of_the_whole_chain(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("mid", model="m")
    await make_agent("leaf", model="f")
    models.models["f"] = echo("leaf says")
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
        call("ask_agent", {"agent": "mid", "message": "research", "thread": "m"}),
        answer_with_tool_results("lead got"),
    )

    parent = await start(container, lead, "go")
    await drain(container)

    assert await last_answer(container, parent.id) == "lead got: mid says: leaf says fact?"


async def test_delegate_task_runs_in_public_thread_without_returning_the_answer(
    container, make_agent, models
):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    models.models["w"] = echo("report:")
    models.models["l"] = steps(
        call("delegate_task", {"agent": "worker", "task": "write it", "thread": "report"}),
        answer_with_tool_results("lead"),
    )

    parent = await start(container, lead, "need report")
    await drain(container)

    assert await last_answer(container, parent.id) == "lead: delegated to worker in thread 'report'"
    [child] = await threads_of(container, parent.id)
    assert child.kind == SessionKind.THREAD and await last_answer(container, child.id) == "report: write it"
    # 公开 thread 的回答推送给用户
    notes = await container.notifications.list(100)
    assert sorted(n.session_id for n in notes) == sorted([parent.id, child.id])


async def test_a_failed_ask_comes_back_as_its_result_and_the_asker_goes_on(container, make_agent, models):
    """对方的运行最终失败：失败原因作为 ask_agent 的结果交回，发问方接着做（不跟着失败）。"""
    lead = (await make_agent("lead", model="l")).id
    await make_agent("broken", model="x")

    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="x", body="bad request")

    models.models["x"] = scripted(fail)
    models.models["l"] = steps(
        call("ask_agent", {"agent": "broken", "message": "do", "thread": "t"}), answer_with_tool_results("x")
    )

    parent = await start(container, lead, "go")
    await drain(container)

    assert [t.status for t in await turns_in(container, parent.id)] == [TaskStatus.DONE, TaskStatus.DONE]
    answer = await last_answer(container, parent.id)
    assert "failed" in answer and "400" in answer
    [child] = await threads_of(container, parent.id)
    [child_run] = await turns_in(container, child.id)
    assert child_run.status == TaskStatus.FAILED and "400" in (child_run.error or "")


async def test_thread_kind_and_owner_are_fixed_at_creation(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    await make_agent("other", model="o")
    errors: list[str] = []
    script = [
        ("ask_agent", {"agent": "worker", "message": "q", "thread": "private"}),
        ("delegate_task", {"agent": "worker", "task": "q", "thread": "public"}),
        ("delegate_task", {"agent": "worker", "task": "q", "thread": "private"}),
        ("ask_agent", {"agent": "worker", "message": "q", "thread": "public"}),
        ("ask_agent", {"agent": "other", "message": "q", "thread": "private"}),
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
    assert "started with ask_agent; use ask_agent" in errors[0]
    assert "started with delegate_task; use delegate_task" in errors[1]
    assert "belongs to another agent" in errors[2]


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
    pub, priv = sorted(
        await threads_of(container, parent.id), key=lambda t: t.kind == SessionKind.PRIVATE_THREAD
    )

    await container.sessions.delete(parent.id)

    async with container.db.transaction() as s:
        assert await s.get(Session, priv.id) is None
        kept = await s.get(Session, pub.id)
        assert kept is not None and kept.parent_session_id is None


async def test_private_thread_is_read_only_for_user_and_hidden_from_list(container, make_agent, models):
    lead = (await make_agent("lead", model="l")).id
    await make_agent("worker", model="w")
    models.models["l"] = steps(
        call("ask_agent", {"agent": "worker", "message": "q", "thread": "t"}), answer_with_tool_results("a")
    )
    parent = await start(container, lead, "go")
    await drain(container)
    [child] = await threads_of(container, parent.id)

    with pytest.raises(Conflict):
        await reply(container, child.id, "hi")
    listed = await container.sessions.list(agent_id=None, origins=None, limit=50, offset=0)
    assert [x.session.id for x in listed] == [parent.id]
    async with container.db.transaction() as s:
        assert not (await s.get(Session, child.id)).unread


async def test_user_may_continue_any_public_session_but_delegation_only_its_own_threads(
    container, make_agent
):
    alpha = (await make_agent("alpha")).id
    cron_session = await start(container, alpha, "scheduled", origin=Origin.TRIGGER)
    # 用户可以在 cron 会话里追问
    await reply(container, cron_session.id, "follow up")
    # 某个会话不能冒充父会话去续写不属于它的会话
    with pytest.raises(Forbidden):
        await container.sessions.receive(
            Inbound(
                origin=Origin.DELEGATION,
                origin_ref="someone-else",
                content="x",
                session=cron_session.id,
                awaited=False,
            )
        )
    with pytest.raises(ValueError, match="only delegation creates threads"):
        await container.sessions.receive(
            Inbound(
                origin=Origin.USER,
                origin_ref=None,
                content="x",
                session=NewSession(agent_id=alpha, title="t", parent_session_id=cron_session.id, thread="t"),
                awaited=True,
            )
        )


async def test_a_running_private_thread_is_not_deleted_with_its_parent_nor_with_its_agent(
    container, make_agent, models
):
    import asyncio
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import FunctionModel

    lead = (await make_agent("lead", model="l")).id
    slow = (await make_agent("slow", model="s")).id
    gate, running = asyncio.Event(), asyncio.Event()

    async def blocked(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
        running.set()
        await gate.wait()
        yield "done"

    models.models["s"] = FunctionModel(stream_function=blocked)
    models.models["l"] = steps(
        call("ask_agent", {"agent": "slow", "message": "q", "thread": "t"}), answer_with_tool_results("a")
    )
    parent = await start(container, lead, "go")
    worker = asyncio.create_task(container.worker.run_forever())
    try:
        async with asyncio.timeout(5):
            await running.wait()
        [child] = await threads_of(container, parent.id)

        # 私有 thread 不能单独删除；随父会话删除时，它正在运行就拒绝
        with pytest.raises(Conflict, match="owned by the session that opened it"):
            await container.sessions.delete(child.id)
        # 父会话也在等这个 thread（它的占用还在）：忙
        with pytest.raises(Conflict, match="busy"):
            await container.sessions.delete(parent.id)
        with pytest.raises(Conflict):
            await container.agent_lifecycle.delete(slow, author="user")
    finally:
        gate.set()
        await idle(container)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert await last_answer(container, parent.id) == "a: done"
    await container.agent_lifecycle.delete(slow, author="user")  # 跑完之后可以删


async def test_delegation_results_point_at_the_thread_they_opened(client, container, make_agent, models):
    from tests.conftest import login
    from tests.test_tools import run_tools

    boss = await make_agent("boss", model="tools")
    await make_agent("helper")
    await run_tools(
        container,
        models,
        boss.id,
        [
            ("delegate_task", {"agent": "helper", "task": "report", "thread": "weekly"}),
            ("ask_agent", {"agent": "helper", "message": "status?", "thread": "check"}),
        ],
    )
    await login(client)
    parent = next(s for s in (await client.get("/api/sessions")).json() if s["parent_session_id"] is None)
    detail = (await client.get(f"/api/sessions/{parent['id']}")).json()
    by_name = {t["thread"]: t["id"] for t in detail["threads"]}
    linked = [(i["tool_name"], i["threads"]) for i in detail["items"] if i["type"] == "tool_result"]
    assert linked == [("delegate_task", [by_name["weekly"]]), ("ask_agent", [by_name["check"]])]


async def test_a_question_after_a_failed_ask_gets_its_own_answer(container, make_agent, models):
    """私有 thread 上一次运行失败、历史停在它保存的请求上：下一次 ask 唤醒的运行先续跑那次请求，再处理新问题，
    新问题拿到它自己的回答。"""
    lead = (await make_agent("lead", model="l")).id
    await make_agent("helper", model="h")
    asks = {"n": 0}

    def lead_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if results := tool_returns(messages):
            return ModelResponse(parts=[TextPart(f"a: {results[0].content}")])
        asks["n"] += 1
        question = last_user_prompt(messages)
        return call("ask_agent", {"agent": "helper", "message": question, "thread": "t"}, f"c{asks['n']}")(
            messages
        )

    helper_calls = {"n": 0}

    def helper_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        helper_calls["n"] += 1
        if helper_calls["n"] == 1:
            raise ModelHTTPError(status_code=400, model_name="h", body="bad request")
        return ModelResponse(parts=[TextPart(f"re {last_user_prompt(messages)}")])

    models.models["l"] = scripted(lead_fn)
    models.models["h"] = scripted(helper_fn)
    parent = await start(container, lead, "first")
    await drain(container)
    assert "failed" in await last_answer(container, parent.id)

    await reply(container, parent.id, "second")
    await drain(container)
    assert await last_answer(container, parent.id) == "a: re second"
    [child] = await threads_of(container, parent.id)
    async with container.db.transaction() as s:
        assert await queued_in(s, child.id) == []
