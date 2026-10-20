"""中途打开会话页：拿到正在进行的这一轮已经产生的运行事件；提交后由数据库接管。"""

import asyncio

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

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
        if live and any(e["event"] == "tool_call" for e in live["events"]):
            break
        await asyncio.sleep(0.02)
    assert live is not None
    kinds = [(e["event"], e["seq"]) for e in live["events"]]
    assert kinds[0] == ("text", 0) and ("tool_call", len(kinds) - 1) in kinds
    assert all(e["run_id"] == live["run_id"] for e in live["events"])

    await worker
    detail = (await client.get(f"/api/sessions/{session_id}")).json()
    assert detail["live"] is None and detail["items"][-1]["text"] == "done"


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
