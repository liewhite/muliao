"""停止正在运行的一轮：停止前已经产生的部分照常保存（下一轮的模型知道做过什么），不推送，展示为「已停止」。"""

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

from muliao.conventions import STOPPED_TOOL_RESULT, TURN_STOPPED
from muliao.models import TaskKind, TaskStatus
from muliao.runtime.stopping import stopped_messages
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, tasks_of


class TwoSteps:
    """先跑一个快的工具，再跑一个慢的；第二轮记下模型看到的历史。"""

    def __init__(self) -> None:
        self.second_turn: list[ModelMessage] | None = None

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
        if prompts[-1] == "next":
            self.second_turn = list(messages)
            return ModelResponse(parts=[TextPart("ok")])
        if isinstance(last, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in last.parts):
            return ModelResponse(
                parts=[
                    TextPart("step two"),
                    ToolCallPart("run_shell", {"command": "sleep 30"}, tool_call_id="slow"),
                ]
            )
        return ModelResponse(
            parts=[
                TextPart("step one"),
                ToolCallPart("run_shell", {"command": "echo quick"}, tool_call_id="fast"),
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
        return live is not None and any(e.get("tool_call_id") == "slow" for e in live.events)

    await _wait_for(slow_tool_running)
    assert (await client.post(f"/api/sessions/{session_id}/stop")).status_code == 202
    await worker

    turns = await tasks_of(container, TaskKind.TURN)
    assert [t.status for t in turns] == [TaskStatus.DONE]
    assert await tasks_of(container, TaskKind.DELIVER) == []  # 用户自己停的，不推送
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert detail["live"] is None
    items = [(i["type"], i["tool_call_id"], i["text"]) for i in detail["items"]]
    assert ("tool_result", "fast", "exit_code: 0\nquick\n") in items
    assert ("tool_result", "slow", STOPPED_TOOL_RESULT) in items
    assert items[-1][0] == "stopped"

    # 下一轮的模型看得到停止前做过的事
    await client.post(f"/api/sessions/{session_id}/messages", json={"prompt": "next"})
    await drain(container)
    seen = [p.content for m in script.second_turn or [] for p in m.parts if isinstance(p, ToolReturnPart)]
    assert "exit_code: 0\nquick\n" in seen and STOPPED_TOOL_RESULT in seen


async def test_stop_without_a_running_turn_is_refused(client, make_agent):
    await login(client)
    agent = await make_agent()
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "hi"})).json()[
        "id"
    ]
    assert (await client.post(f"/api/sessions/{session_id}/stop")).status_code == 409


def test_a_turn_stopped_before_its_first_model_request_keeps_the_user_message():
    messages = stopped_messages(None, "hello", None)
    assert [type(p).__name__ for m in messages for p in m.parts] == ["UserPromptPart"]
    assert messages[-1].metadata == {TURN_STOPPED: True}
    assert stopped_messages(None, None, None) == []
    # pydantic-ai 停在处理挂起结果的途中时给出的是空列表，同样保留用户消息
    assert stopped_messages([], "hello", None)[0].parts[0].content == "hello"


def test_a_turn_stopped_early_still_closes_the_questions_it_was_answering():
    """停在第一次请求之前，也要把挂起提问的结果（回答或「没回答」）存下：否则历史里的提问一直没有结果，
    下一轮会把结果接在用户消息之后，提供方拒绝这种顺序，会话从此每一轮都失败。"""
    from pydantic_ai.tools import DeferredToolResults

    from muliao.runtime.questions import NOT_ANSWERED, pending_questions

    asked = ModelResponse(
        parts=[
            ToolCallPart(
                "ask_user",
                {"questions": [{"question": "q", "header": "h", "options": [{"label": "a"}]}]},
                tool_call_id="q1",
            )
        ]
    )
    deferred = DeferredToolResults(calls={"q1": NOT_ANSWERED.model_dump()})

    messages = stopped_messages(None, "直接分析", deferred)

    [request] = messages
    assert [type(p).__name__ for p in request.parts] == ["ToolReturnPart", "UserPromptPart"]
    assert request.parts[0].tool_call_id == "q1" and request.parts[0].content == NOT_ANSWERED.model_dump()
    assert request.metadata == {TURN_STOPPED: True}
    assert pending_questions([asked, *messages]) == {}


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
        return live is not None and any(e.get("tool_call_id") == "slow" for e in live.events)

    await _wait_for(slow_tool_running)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    # 关停打断的一轮不留任何记录，下次启动由队列恢复重跑
    assert [t.status for t in await tasks_of(container, TaskKind.TURN)] == [TaskStatus.RUNNING]
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert [i["type"] for i in detail["items"]] == [] and detail["live"] is None
    assert not container.turns.stop(session_id)


async def test_stopping_mid_stream_keeps_the_partial_answer_and_never_stores_a_truncated_secret(
    client, container, make_agent, models
):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel
    from sqlalchemy import select

    from muliao.models import Message

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
        return live is not None and any(e["event"] == "text" for e in live.events)

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


async def test_stoppable_runs_tell_a_user_stop_from_an_outer_cancellation():
    import pytest

    from muliao.runtime.stopping import StoppableRuns, Stopped

    runs: StoppableRuns[str] = StoppableRuns()
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

    # 用户停止：只取消运行本身，结果是 Stopped
    stopped = asyncio.create_task(runs.run("s", forever))
    await started.wait()
    assert runs.stop("s")
    assert isinstance(await stopped, Stopped)
    assert cancelled == [True] and not runs.stop("s")

    # 外层被取消（关停）：运行一起取消，取消照常抛出
    started.clear()
    outer = asyncio.create_task(runs.run("s", forever))
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert cancelled == [True, True] and not runs.stop("s")


async def test_a_second_run_in_the_same_session_is_refused():
    import pytest

    from muliao.runtime.stopping import StoppableRuns

    runs: StoppableRuns[str] = StoppableRuns()
    gate = asyncio.Event()

    async def wait() -> str:
        await gate.wait()
        return "done"

    first = asyncio.create_task(runs.run("s", wait))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="already has a running turn"):
        await runs.run("s", wait)
    gate.set()
    assert await first == "done"


def test_a_turn_stopped_while_handling_answers_keeps_the_answers_and_the_message():
    """停在处理挂起结果的途中：pydantic-ai 只留下一条被打断的空请求（不含用户消息），回答与消息都要补上。"""
    from pydantic_ai.tools import DeferredToolResults

    from muliao.runtime.questions import NOT_ANSWERED

    deferred = DeferredToolResults(calls={"q1": NOT_ANSWERED.model_dump(), "q2": NOT_ANSWERED.model_dump()})
    # q1 的结果已经处理完记下了，q2 还没有
    interrupted = ModelRequest(
        parts=[ToolReturnPart("ask_user", NOT_ANSWERED.model_dump(), tool_call_id="q1")], state="interrupted"
    )

    messages = stopped_messages([interrupted], "直接分析", deferred)

    returns = [p.tool_call_id for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
    prompts = [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
    assert sorted(returns) == ["q1", "q2"] and prompts == ["直接分析"]
    assert (
        stopped_messages([ModelRequest(parts=[], state="interrupted")], "hi", None)[0].parts[0].content
        == "hi"
    )
