"""中途打开会话页：拿到正在进行的这一轮已经产生的运行事件；提交后由数据库接管。"""

import asyncio
from datetime import datetime

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

from muliao.sessions.transcript import result_item
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, tool_returns


async def test_a_page_opened_mid_turn_sees_the_turn_from_its_start(client, container, make_agent, models):
    await login(client)
    agent = await make_agent("worker", model="slow")

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if tool_returns(messages):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(
            parts=[TextPart("checking"), ToolCallPart("run_shell", {"command": "sleep 1"}, tool_call_id="s")]
        )

    models.models["slow"] = scripted(fn)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "go"})).json()[
        "id"
    ]
    worker = asyncio.create_task(drain(container))

    live = None
    for _ in range(100):
        live = (await client.get(f"/api/sessions/{session_id}")).json()["live"]
        if live and any(_label(e) == "tool_call" for e in live["events"]):
            break
        await asyncio.sleep(0.02)
    assert live is not None
    kinds = [(_label(e), e["seq"]) for e in live["events"]]
    assert kinds[0] == ("assistant", 0) and ("tool_call", len(kinds) - 1) in kinds
    assert all(e["run_id"] == live["run_id"] for e in live["events"])

    await worker
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert detail["live"] is None and detail["items"][-1]["text"] == "done"

    # 工具调用带开始执行的时间：运行中的事件与保存后的历史给的是同一个时刻（前者收到调用事件时、后者回复保存时，
    # 都在执行之前）；结果带结束的时间，两者之差就是工具真实的耗时
    live_call = next(e["item"] for e in live["events"] if _label(e) == "tool_call")
    call, result = (i for i in detail["items"] if i["tool_call_id"] == "s")
    started, saved_start = datetime.fromisoformat(live_call["at"]), datetime.fromisoformat(call["at"])
    assert abs((started - saved_start).total_seconds()) < 0.5
    assert (datetime.fromisoformat(result["at"]) - saved_start).total_seconds() >= 0.9
    assert all(i["at"] is None for i in detail["items"] if i["tool_call_id"] is None)


async def test_a_failed_turn_leaves_no_live_events_behind(client, container, make_agent, models):
    await login(client)
    agent = await make_agent("worker", model="broken")
    calls = 0

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[TextPart("trying"), ToolCallPart("list_agents", {}, tool_call_id="l")]
            )
        raise RuntimeError("model is down")

    models.models["broken"] = scripted(fn)
    session_id = (await client.post("/api/sessions", json={"agent_id": agent.id, "prompt": "go"})).json()[
        "id"
    ]
    await drain(container)

    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert detail["run"]["status"] == "failed"
    assert detail["live"] is None
    assert container.live.snapshot(session_id) is None


def test_a_result_made_up_for_an_interrupted_call_has_no_end_time():
    # 补上的结果是重启后才合成的，它的 timestamp 不是调用结束的时间
    interrupted = ToolReturnPart("run_shell", "Interrupted", tool_call_id="x", outcome="interrupted")
    done = ToolReturnPart("run_shell", "ok", tool_call_id="y")
    assert result_item(interrupted).at is None
    assert result_item(done).at == done.timestamp


def _label(event: dict) -> str:
    """实时事件是哪种条目：流式文字的类型，或完整条目的类型。"""
    return event["item_type"] if event["event"] == "delta" else event["item"]["type"]
