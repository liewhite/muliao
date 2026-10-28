"""停止会话：正在进行的运行结束，停止前已经产生的部分照常保存（之后的模型知道做过什么），不推送，展示为「已停止」；
信箱清空（没交给模型的信件记为被丢弃）。"""

import asyncio

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo

from muliao.notifications.push import DELIVER_TASK
from muliao.platform.tables import TaskStatus
from muliao.sessions.conventions import RUN_STOPPED, STOPPED_TOOL_RESULT
from muliao.sessions.lanes import RUN_TASK
from muliao.sessions.mailbox import post_in
from muliao.sessions.tables import MailKind, MailState
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, history_of, mail_of, tasks_of


class TwoSteps:
    """先跑一个快的工具，再跑一个慢的；第二轮记下模型看到的历史。"""

    def __init__(self) -> None:
        self.second_turn: list[ModelMessage] | None = None

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
        if prompts[-1] == ["next"]:
            self.second_turn = list(messages)
            return ModelResponse(parts=[TextPart("ok")])
        if isinstance(last, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in last.parts):
            return ModelResponse(
                parts=[
                    TextPart("step two"),
                    ToolCallPart("run_shell", {"command": "sleep 30", "timeout": 60}, tool_call_id="slow"),
                ]
            )
        return ModelResponse(
            parts=[
                TextPart("step one"),
                ToolCallPart("run_shell", {"command": "echo quick", "timeout": 60}, tool_call_id="fast"),
            ]
        )


async def _wait_for(predicate) -> None:
    for _ in range(250):
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_stopping_keeps_what_already_happened_and_marks_the_turn_stopped(
    client, container, make_agent, models
):
    await login(client)
    agent = await make_agent("worker", model="steps")
    script = TwoSteps()
    models.models["steps"] = scripted(script)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "go"})).json()[
        "id"
    ]
    worker = asyncio.create_task(drain(container, limit_seconds=20))

    async def slow_tool_running() -> bool:
        live = container.live.snapshot(session_id)
        return live is not None and any(
            e.event == "item" and e.item.tool_call_id == "slow" for e in live.events
        )

    await _wait_for(slow_tool_running)
    # 慢工具还在跑时到达的事件：还没交给模型
    async with container.db.transaction() as s:
        await post_in(
            s, container.queue, session_id, kind=MailKind.EVENT, source="job:9", content="done", awaited=False
        )
    assert (await client.post(f"/api/sessions/{session_id}/stop")).status_code == 202
    await worker

    turns = await tasks_of(container, RUN_TASK)
    assert [t.status for t in turns] == [TaskStatus.DONE]
    # 信箱清空：事件记为被丢弃，不唤醒新的运行；界面上显示为被丢弃
    assert [(m.content, m.state) for m in await mail_of(container)][-1] == ("done", MailState.DISCARDED)
    assert await tasks_of(container, DELIVER_TASK) == []  # 用户自己停的，不推送
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert detail["live"] is None and detail["run"] is None
    assert [(m["content"], m["state"]) for m in detail["mail"]] == [("done", "discarded")]
    items = [(i["type"], i["tool_call_id"], i["text"]) for i in detail["items"]]
    assert ("tool_result", "fast", "exit_code: 0\nquick\n") in items
    assert ("tool_result", "slow", STOPPED_TOOL_RESULT) in items
    assert items[-1][0] == "stopped"

    # 下一轮的模型看得到停止前做过的事
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "next"})
    await drain(container)
    seen = [p.content for m in script.second_turn or [] for p in m.parts if isinstance(p, ToolReturnPart)]
    assert "exit_code: 0\nquick\n" in seen and STOPPED_TOOL_RESULT in seen


async def test_stopping_an_idle_session_clears_its_mailbox(client, container, make_agent, models):
    await login(client)
    agent = await make_agent("worker", model="never")
    called: list[bool] = []

    def never(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        called.append(True)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["never"] = scripted(never)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "hi"})).json()[
        "id"
    ]

    # 消息还排着、没有在运行：停止就是清空信箱，已经投递的运行开始时没有可交给模型的
    assert (await client.post(f"/api/sessions/{session_id}/stop")).status_code == 202
    await drain(container)

    assert called == []
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert [(m["content"], m["state"]) for m in detail["mail"]] == [("hi", "discarded")]
    assert detail["items"] == [] and detail["run"] is None
    # 之后的消息照常唤醒
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "again"})
    await drain(container)
    assert called == [True]
    assert (await client.get(f"/api/sessions/{session_id}")).json()["mail"] == []


async def test_shutdown_is_not_a_stop_the_turn_is_left_for_recovery(client, container, make_agent, models):
    await login(client)
    agent = await make_agent("worker", model="steps")
    models.models["steps"] = scripted(TwoSteps())
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "go"})).json()[
        "id"
    ]
    worker = asyncio.create_task(container.worker.run_forever())

    async def slow_tool_running() -> bool:
        live = container.live.snapshot(session_id)
        return live is not None and any(
            e.event == "item" and e.item.tool_call_id == "slow" for e in live.events
        )

    await _wait_for(slow_tool_running)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    # 关停不是停止：已经保存的步骤留着，不标「已停止」，下次启动由队列恢复、从上一步续跑
    assert [t.status for t in await tasks_of(container, RUN_TASK)] == [TaskStatus.RUNNING]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    items = [(i["type"], i["tool_call_id"]) for i in detail["items"]]
    assert ("tool_result", "fast") in items and "stopped" not in [kind for kind, _ in items]
    assert detail["live"] is None


async def test_stopping_mid_stream_keeps_the_partial_answer_and_never_stores_a_truncated_secret(
    client, container, make_agent, models
):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel
    from sqlalchemy import select

    from muliao.sessions.tables import Message

    await login(client)
    agent = await make_agent("keeper", model="streaming")
    hang = asyncio.Event()

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        yield "saving your token now"
        # 参数生成到一半：密钥只出来了一截
        yield {
            1: DeltaToolCall(name="vault_set", json_args='{"key": "T", "value": "super-sec', tool_call_id="v")
        }
        await hang.wait()

    models.models["streaming"] = FunctionModel(
        lambda _m, _i: ModelResponse(parts=[TextPart("unused")]), stream_function=stream
    )
    session_id = (
        await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "store it"})
    ).json()["id"]
    worker = asyncio.create_task(drain(container, limit_seconds=20))

    async def streaming() -> bool:
        live = container.live.snapshot(session_id)
        return live is not None and any(
            e.event == "delta" and e.item_type == "assistant" for e in live.events
        )

    await _wait_for(streaming)
    await asyncio.sleep(0.1)  # 让工具调用的增量也进入运行历史
    assert (await client.post(f"/api/sessions/{session_id}/stop")).status_code == 202
    await worker

    async with container.db.transaction() as s:
        payloads = (await s.scalars(select(Message.payload).where(Message.session_id == session_id))).all()
    assert payloads and not any("super-sec" in p for p in payloads)
    items = [
        (i["type"], i["text"]) for i in (await client.get(f"/api/sessions/{session_id}")).json()["items"]
    ]
    assert ("assistant", "saving your token now") in items
    assert items[-1] == ("stopped", "")


async def test_stoppable_runs_tell_a_user_stop_from_an_outer_cancellation(container):
    import pytest

    from muliao.sessions.stopping import StoppableRuns

    runs: StoppableRuns[str] = StoppableRuns(container.db.exclusive)
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def forever() -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return "never"

    async def wind_up() -> str:
        return "stopped"

    # 用户停止：只取消运行本身，结果是停止后收尾的结果
    stopped = asyncio.create_task(runs.run("s", forever, wind_up))
    await started.wait()
    assert await runs.stop("s")
    assert await stopped == "stopped"
    assert cancelled == [True] and not await runs.stop("s")

    # 外层被取消（关停）：运行一起取消，取消照常抛出
    started.clear()
    outer = asyncio.create_task(runs.run("s", forever, wind_up))
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert cancelled == [True, True] and not await runs.stop("s")


async def test_a_second_run_in_the_same_session_is_refused(container):
    import pytest

    from muliao.sessions.stopping import StoppableRuns

    runs: StoppableRuns[str] = StoppableRuns(container.db.exclusive)
    gate = asyncio.Event()

    async def wait() -> str:
        await gate.wait()
        return "done"

    first = asyncio.create_task(runs.run("s", wait, wait))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="already has a running"):
        await runs.run("s", wait, wait)
    gate.set()
    assert await first == "done"


async def _failed_after_saving_a_request(container, make_agent, models) -> tuple[str, list[int]]:
    """会话的运行把第一次请求保存后在模型处失败：历史停在一条会被续跑重发的请求上。返回会话与模型调用计数。"""
    from pydantic_ai.exceptions import ModelHTTPError

    from tests.test_flow import start

    agent = await make_agent("worker", model="flaky")
    calls: list[int] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls.append(1)
        if len(calls) == 1:
            raise ModelHTTPError(status_code=400, model_name="flaky", body="bad request")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["flaky"] = scripted(fn)
    session = await start(container, agent.id, "go")
    await drain(container)
    return session.id, calls


async def test_stopping_during_a_pending_retry_stops_the_retry(container, make_agent, models):
    from datetime import timedelta

    from muliao.platform.tables import Task

    session_id, calls = await _failed_after_saving_a_request(container, make_agent, models)
    # 如同在退避里等重试：排着、还没到时间
    [run] = await tasks_of(container, RUN_TASK)
    async with container.db.transaction() as s:
        task = await s.get(Task, run.id)
        task.status, task.not_before = TaskStatus.PENDING, container.queue.now() + timedelta(hours=1)

    await container.stopper(session_id)
    await drain(container)

    assert [t.status for t in await tasks_of(container, RUN_TASK)] == [TaskStatus.DONE]
    history = await history_of(container, session_id)
    assert history[-1].metadata[RUN_STOPPED] is True
    # 之后的消息不会把停下的请求再发一遍
    from tests.test_flow import reply

    await reply(container, session_id, "next")
    await drain(container)
    assert len(calls) == 2
    history = await history_of(container, session_id)
    assert [type(m).__name__ for m in history] == ["ModelRequest", "ModelRequest", "ModelResponse"]


async def test_stopping_a_resumed_run_before_its_first_step_closes_the_history(
    container, make_agent, models, monkeypatch
):
    """停在运行拿到运行中的历史之前（组装指令、连接 MCP 期间）：从库里的历史补上停止，续跑不会重发停下的请求。"""
    from muliao.agent_runtime.runner import AgentRuntime
    from tests.test_flow import reply

    session_id, calls = await _failed_after_saving_a_request(container, make_agent, models)
    entered, gate = asyncio.Event(), asyncio.Event()
    original = AgentRuntime.instructions

    async def slow(self, agent, scope, summary, origin_note):  # noqa: ANN001, ANN202
        entered.set()
        await gate.wait()
        return await original(self, agent, scope, summary, origin_note)

    monkeypatch.setattr(AgentRuntime, "instructions", slow)
    await reply(container, session_id, "again")
    worker = asyncio.create_task(drain(container))
    async with asyncio.timeout(5):
        await entered.wait()
    await container.stopper(session_id)
    await worker

    assert len(calls) == 1
    history = await history_of(container, session_id)
    assert [type(m).__name__ for m in history] == ["ModelRequest"]
    assert history[-1].metadata[RUN_STOPPED] is True
    assert [m.state for m in await mail_of(container)][-1] == MailState.DISCARDED


async def test_stopping_a_run_before_it_reads_its_state_still_stops_the_session(
    container, make_agent, models, monkeypatch
):
    import muliao.sessions.consumer as consumer_module
    from tests.test_flow import start

    agent = await make_agent("worker", model="never")
    called: list[bool] = []
    models.models["never"] = scripted(
        lambda m, i: called.append(True) or ModelResponse(parts=[TextPart("x")])
    )
    entered, gate = asyncio.Event(), asyncio.Event()
    original = consumer_module.load_context

    async def slow(s, session_id):  # noqa: ANN001, ANN202
        entered.set()
        await gate.wait()
        return await original(s, session_id)

    monkeypatch.setattr(consumer_module, "load_context", slow)
    session = await start(container, agent.id, "hi")
    worker = asyncio.create_task(drain(container))
    async with asyncio.timeout(5):
        await entered.wait()
    # 停止要等进行中的事务做完才发出取消（db.Database.exclusive）：放开事务，停止随即生效
    stopping = asyncio.create_task(container.stopper(session.id))
    await asyncio.sleep(0.05)
    assert not stopping.done()
    gate.set()
    await stopping
    await worker

    assert called == []
    assert [t.status for t in await tasks_of(container, RUN_TASK)] == [TaskStatus.DONE]
    assert [m.state for m in await mail_of(container)] == [MailState.DISCARDED]
