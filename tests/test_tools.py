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


async def test_temp_agent_runs_once_in_a_discarded_workspace_and_returns_its_answer(
    container, make_agent, models
):
    me = await make_agent("manager", model="tools")
    await container.vault.set(me.id, "API_KEY", "k-123")
    seen: dict[str, object] = {}

    def helper(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.setdefault("tools", sorted(t.name for t in info.function_tools))
        seen.setdefault("instructions", info.instructions or "")
        step = sum(isinstance(m, ModelResponse) for m in messages)
        if step == 0:
            return ModelResponse(
                parts=[ToolCallPart("write_file", {"path": "notes.txt", "content": "x"}, tool_call_id="w")]
            )
        if step == 1:
            command = 'pwd && test -n "$API_KEY" && echo has-key'
            return ModelResponse(
                parts=[
                    ToolCallPart("run_shell", {"command": command, "secrets": ["API_KEY"]}, tool_call_id="s")
                ]
            )
        seen["shell"] = str(tool_returns(messages)[0].content)
        return ModelResponse(parts=[TextPart("summary: page says hello")])

    models.models["helper"] = scripted(helper)
    outputs = await run_tools(
        container,
        models,
        me.id,
        [("run_temp_agent", {"task": "summarize https://example.com", "model": "helper"})],
    )

    assert outputs == ["summary: page says hello"]
    workspace = str(seen["shell"]).splitlines()[1]
    assert workspace.startswith(str(container.settings.temp_dir)) and "has-key" in str(seen["shell"])
    assert workspace and list(container.settings.temp_dir.iterdir()) == []  # noqa: ASYNC240  测试里的同步检查
    # 只有文件与 shell：没有记忆、skill、委派，也不能再开临时 agent
    assert seen["tools"] == ["edit_file", "list_files", "read_file", "run_shell", "write_file"]
    assert "temporary helper agent working for agent 'manager'" in str(seen["instructions"])
    # 不留下会话
    assert len(await container.sessions.list(agent_id=None, origins=None, limit=10, offset=0)) == 1


async def test_parallel_partial_updates_of_one_agent_both_survive(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    target = await make_agent("worker")

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "update_agent", {"agent": "worker", "description": "new desc"}, tool_call_id="a"
                    ),
                    ToolCallPart("update_agent", {"agent": "worker", "reflect_every": 7}, tool_call_id="b"),
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    models.models["tools"] = scripted(fn)
    await start(container, me.id, "go", origin=Origin.CRON)
    await drain(container)

    updated = await container.agents.get(target.id)
    assert (updated.description, updated.reflect_every) == ("new desc", 7)
    # 未给出的指令不被改写：不追加版本
    assert len(await container.prompts.history(target.id)) == 1


async def test_tool_errors_name_agents_but_never_expose_ids(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    other = await make_agent("worker")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("create_agent", {"name": "worker", "description": "d", "instructions": ""}),
            ("update_agent", {"agent": "manager", "new_name": "worker"}),
            ("delete_agent", {"agent": "manager"}),
        ],
    )

    assert "already taken" in outputs[0] and "already taken" in outputs[1]
    assert "cannot delete yourself" in outputs[2]
    for agent_id in (me.id, other.id):
        assert agent_id not in "\n".join(outputs)


async def test_event_trigger_token_never_overwrites_and_moves_with_rotate_and_delete(
    container, make_agent, models
):
    me = await make_agent("manager", model="tools")
    await container.vault.set(me.id, token_key("mine"), "user-secret")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("event_trigger_create", {"name": "mine", "prompt": "p"}),
            ("event_trigger_create", {"name": "a_b", "prompt": "p"}),
            ("event_trigger_create", {"name": "hook", "prompt": "p"}),
            ("event_trigger_rotate_token", {"name": "hook"}),
            ("event_trigger_delete", {"name": "hook"}),
        ],
    )

    # 已有同名 secret 时不创建触发器、不覆盖
    assert "already has EVENT_TOKEN_MINE" in outputs[0]
    assert await container.vault.resolve_many(me.id, [token_key("mine")]) == {
        token_key("mine"): "user-secret"
    }
    assert [t.name for t in await container.events.list()] == []
    # 名称不允许下划线，名称到 vault key 一一对应
    assert "invalid event trigger name" in outputs[1]
    assert "any other copy is now invalid" in outputs[3]
    assert "and its token EVENT_TOKEN_HOOK" in outputs[4]
    assert not await container.vault.has(me.id, token_key("hook"))
