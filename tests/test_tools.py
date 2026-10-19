"""agent 能用工具完成的系统操作：管理 agent 与公共 prompt、MCP 启停、cron 修改、事件触发器、vault、thread 列表。"""

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo

from muliao.models import McpTransport, Origin
from muliao.runtime.mcp import McpSpec
from muliao.runtime.tools.triggers import token_key
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start, tool_returns


async def run_tools(container, models, agent_id: str, calls: list[tuple[str, dict]]) -> list[str]:
    """让 agent 依次调用这些工具（每次一个），返回每次调用的结果或交还给模型的错误。"""
    outputs: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            outputs.extend(str(p.content) for p in tool_returns(messages))
            outputs.extend(str(p.content) for p in last.parts if isinstance(p, RetryPromptPart))
        step = sum(isinstance(m, ModelResponse) for m in messages)
        if step < len(calls):
            name, args = calls[step]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{step}")])
        return ModelResponse(parts=[TextPart("done")])

    models.models["tools"] = scripted(fn)
    await start(container, agent_id, "do it", origin=Origin.CRON)
    await drain(container)
    return outputs


async def test_agent_creates_and_reconfigures_agents_with_versioned_instructions(
    container, make_agent, models
):
    me = await make_agent("manager", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "create_agent",
                {"name": "researcher", "description": "finds facts", "instructions": "Cite sources."},
            ),
            ("update_agent", {"agent": "manager", "instructions": "Delegate research to researcher."}),
            ("get_agent", {"agent": "researcher"}),
            ("delete_agent", {"agent": "researcher"}),
        ],
    )

    assert outputs[0] == "created agent researcher"
    assert "Cite sources." in outputs[2] and "fake:default" in outputs[2]  # 未指定模型时取服务端默认值
    assert outputs[3] == "deleted agent researcher"
    assert [a.name for a in await container.agents.list()] == ["manager"]
    # 自己改自己的指令：留有版本，作者是自己
    history = await container.prompts.history(me.id)
    assert [(r.content, r.author) for r in history][0] == ("Delegate research to researcher.", me.id)
    # 结果里只出现名称，不出现 id
    assert me.id not in "\n".join(outputs)


async def test_agent_edits_public_prompt(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("get_public_prompt", {}),
            ("set_public_prompt", {"content": "Be brief."}),
            ("get_public_prompt", {}),
        ],
    )

    assert outputs == ["(no public prompt)", "public prompt updated", "Be brief."]
    assert (await container.prompts.history(None))[0].author == me.id


async def test_agent_pauses_cron_job_and_reenables_mcp_server(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    await container.cron.create(name="daily", agent_id=me.id, schedule="0 9 * * *", prompt="report")
    spec = McpSpec(
        name="echo", transport=McpTransport.HTTP, command=None, args=(), url="http://x", env=(), headers=()
    )
    await container.mcp.add(me.id, spec, probe=False)
    # 停用的 server 不会在这一轮被连接；agent 看得到它并重新启用（下一轮起生效）
    await container.mcp.set_enabled(me.id, "echo", False)

    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("cron_update", {"name": "daily", "enabled": False}),
            ("mcp_list", {}),
            ("mcp_set_enabled", {"name": "echo", "enabled": True}),
        ],
    )

    assert outputs[0] == "updated cron job daily, paused"
    assert not (await container.cron.get_by_name("daily")).enabled
    assert "echo" in outputs[1]
    assert [m.name for m in await container.mcp.visible_to(me.id)] == ["echo"]


async def test_event_trigger_token_goes_to_creators_private_vault(client, container, make_agent, models):
    me = await make_agent("manager", model="tools")
    outputs = await run_tools(
        container, models, me.id, [("event_trigger_create", {"name": "github", "prompt": "Triage it."})]
    )

    key = token_key("github")
    assert key in outputs[0] and "http://test/api/hooks/github" in outputs[0]
    token = (await container.vault.resolve_many(me.id, [key]))[key]
    assert token not in outputs[0]
    await login(client)
    resp = await client.post(
        "/api/hooks/github", content="opened", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 202


async def test_agent_manages_private_and_public_secrets(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    other = await make_agent("other")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("vault_set", {"key": "A", "value": "private-a", "description": "mine"}),
            ("vault_set", {"key": "B", "value": "public-b", "description": "shared", "scope": "public"}),
            ("vault_delete", {"key": "A"}),
            ("vault_list", {}),
        ],
    )

    assert "private-a" not in "\n".join(outputs) and "public-b" not in "\n".join(outputs)
    assert await container.vault.resolve_many(other.id, ["B"]) == {"B": "public-b"}
    assert [s.key for s in await container.vault.visible_to(me.id)] == ["B"]


async def test_list_threads_shows_each_thread_with_its_state(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    await make_agent("worker")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("ask_agent", {"agent": "worker", "message": "q", "thread": "review"}),
            ("list_threads", {}),
        ],
    )

    assert outputs[0] == "ok"
    assert "review" in outputs[1] and "worker" in outputs[1] and "done" in outputs[1]
