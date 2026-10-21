"""运行上下文与缓存前缀：instructions 只放同一个会话不会变的内容，当前时间这类随运行变化的内容挂在这次运行
新增的第一条请求上，已经保存的消息永远不改（见 docs/architecture.md 第 6 节）。"""

from datetime import UTC, datetime

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.agent_runtime.conventions import run_context_index
from muliao.platform.tables import TaskStatus
from muliao.platform.tooling import Audience
from muliao.sessions.api import Origin
from muliao.sessions.lanes import RUN_TASK
from muliao.sessions.tables import Message
from tests.scripted import scripted
from tests.test_flow import drain, history_of, start, tasks_of

_TIME = "[Current local time: "


async def payloads_of(container, session_id: str) -> list[str]:
    async with container.db.transaction() as s:
        query = select(Message.payload).where(Message.session_id == session_id).order_by(Message.id)
        return list((await s.scalars(query)).all())


async def test_instructions_are_the_same_at_any_time_and_for_any_audience(container, make_agent):
    """instructions 是缓存前缀：同一个 agent 在不同时间、不同听众下组装出的指令必须一字不差。"""
    agent = await make_agent("alpha")
    runtime = container.consumer._runtime  # noqa: SLF001
    scope = runtime.scope(agent, session_id="s1", audience=Audience.USER)

    runtime._clock = lambda: datetime(2026, 1, 1, 9, 0, tzinfo=UTC)  # noqa: SLF001
    morning = await runtime.instructions(agent, scope, None, "note")
    # 一天之后、而且这次运行没有人在等回答（Audience 随这次处理的信件变，不能进指令）
    runtime._clock = lambda: datetime(2026, 1, 2, 23, 30, tzinfo=UTC)  # noqa: SLF001
    later = runtime.scope(agent, session_id="s1", audience=Audience.NONE)
    evening = await runtime.instructions(agent, later, None, "note")

    assert morning == evening
    assert _TIME not in morning and "notified" not in morning


async def test_a_run_hands_the_model_the_time_in_its_first_request(container, make_agent, models):
    seen: list[list[ModelMessage]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    agent = await make_agent("alpha", model="m")
    session = await start(container, agent.id, "hi")
    await drain(container)

    # 模型收到的第一条请求：运行上下文在用户自己的内容之前，各占一段
    [request] = seen[0]
    assert isinstance(request, ModelRequest) and run_context_index(request) == 0
    assert request.parts[0].content.startswith(_TIME)
    assert "The user is notified with your final answer" in request.parts[0].content
    assert request.parts[1].content == ["hi"]

    # 入库的是同一条，之后原样重放；但会话条目里不展示这一段（它不是用户说的话）
    history = await history_of(container, session.id)
    assert run_context_index(history[0]) == 0
    items = (await container.sessions.detail(session.id)).items
    assert [(i.type, i.text) for i in items] == [("user", "hi"), ("assistant", "ok")]


async def test_resuming_resends_the_saved_request_unchanged_and_marks_the_next_new_one(
    container, make_agent, models
):
    """续跑时重发的是已经保存的那条请求：一个字节都不改（改了它，它之前的缓存就全废了），这次运行的运行上下文
    挂到它新增的第一条请求上。"""
    agent = await make_agent("alpha", model="flaky")
    calls = {"n": 0}

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[ToolCallPart("memory_append", {"entry": "- one"}, tool_call_id="m1")])
        if calls["n"] == 2:
            raise ModelHTTPError(status_code=400, model_name="flaky", body="bad request")
        if calls["n"] == 3:
            return ModelResponse(parts=[ToolCallPart("memory_append", {"entry": "- two"}, tool_call_id="m2")])
        return ModelResponse(parts=[TextPart("done")])

    models.models["flaky"] = scripted(fn)
    session = await start(container, agent.id, "remember", origin=Origin.TRIGGER)
    await drain(container)
    [failed] = await tasks_of(container, RUN_TASK)
    assert failed.status == TaskStatus.FAILED
    before = await payloads_of(container, session.id)

    await container.queue.retry(failed.id)
    await drain(container)

    after = await payloads_of(container, session.id)
    assert after[: len(before)] == before
    history = await history_of(container, session.id)
    requests = [m for m in history if isinstance(m, ModelRequest)]
    # 第一次运行只在它新增的第一条请求（用户内容）上挂；第二条是它新增的第二条，不挂。续跑那次重发第二条、
    # 不改它，把自己的运行上下文挂到第三条上——那条只有工具结果，这一段排在工具结果之后
    assert [run_context_index(r) for r in requests] == [0, None, 1]
    assert requests[2].parts[1].content.startswith(_TIME)
