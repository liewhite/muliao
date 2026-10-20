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

from muliao.models import Origin
from muliao.runtime.mcp import McpTransport, mcp_config
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
    await start(container, agent_id, "do it", origin=Origin.TRIGGER)
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
    [latest, *_] = await container.prompts.history(me.id, 10)
    assert (container.prompts.current(me.id), latest.author) == ("Delegate research to researcher.", me.id)
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
    assert (await container.prompts.history(None, 10))[0].author == me.id


async def test_agent_pauses_trigger_and_reenables_mcp_server(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    await container.triggers.create(
        name="daily", agent_id=me.id, kind="schedule", prompt="report", config={"cron": "0 9 * * *"}
    )
    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add(me.id, "echo", spec, probe=False, author="user")
    # 停用的 server 不会在这一轮被连接；agent 看得到它并重新启用（下一轮起生效）
    await container.mcp.set_enabled(me.id, "echo", False, "user")

    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("trigger_update", {"name": "daily", "enabled": False}),
            ("mcp_list", {}),
            ("mcp_set_enabled", {"name": "echo", "enabled": True}),
        ],
    )

    assert outputs[0].startswith("updated trigger daily (paused)")
    assert not (await container.triggers.get_by_name("daily")).enabled
    assert "echo" in outputs[1]
    assert [m.name for m in container.mcp.visible_to(me.id)] == ["echo"]


async def test_agent_creates_webhook_but_only_the_user_can_issue_its_token(
    client, container, make_agent, models
):
    me = await make_agent("manager", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("trigger_kinds", {}),
            ("trigger_create", {"name": "github", "kind": "webhook", "prompt": "Triage it.", "config": {}}),
            (
                "trigger_create",
                {"name": "daily", "kind": "schedule", "prompt": "Report.", "config": {"cron": "bad"}},
            ),
        ],
    )

    assert "schedule" in outputs[0] and "webhook" in outputs[0]
    assert "http://test/api/hooks/github" in outputs[1] and "'has_token': False" in outputs[1]
    assert "invalid cron expression" in outputs[2]
    # 没有 token 时收不到事件；用户在界面上生成后才能用，token 不经过 agent 与 vault
    await login(client)
    assert (
        await client.post("/api/hooks/github", content="x", headers={"Authorization": "Bearer x"})
    ).status_code == 401
    trigger = await container.triggers.get_by_name("github")
    token = (await client.post(f"/api/triggers/{trigger.id}/webhook/token")).json()["token"]
    resp = await client.post(
        "/api/hooks/github", content="opened", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 202
    assert await container.vault.list_all() == []


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
    assert seen["tools"] == ["edit_file", "list_files", "read_file", "run_shell", "share_file", "write_file"]
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
    await start(container, me.id, "go", origin=Origin.TRIGGER)
    await drain(container)

    updated = await container.agents.get(target.id)
    assert (updated.description, updated.reflect_every) == ("new desc", 7)
    # 未给出的指令不被改写：不追加版本
    assert len(await container.prompts.history(target.id, 10)) == 1


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


async def test_failed_temp_agent_still_removes_its_workspace_and_fails_the_turn(
    container, make_agent, models
):
    from pydantic_ai.exceptions import ModelHTTPError

    from muliao.models import TaskKind, TaskStatus
    from tests.test_flow import tasks_of

    me = await make_agent("manager", model="tools")

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="helper", body="bad request")

    models.models["helper"] = scripted(broken)
    await run_tools(container, models, me.id, [("run_temp_agent", {"task": "x", "model": "helper"})])

    assert list(container.settings.temp_dir.iterdir()) == []  # noqa: ASYNC240
    [turn] = await tasks_of(container, TaskKind.RUN)
    assert turn.status == TaskStatus.FAILED and "400" in turn.error


async def test_temp_agent_over_request_limit_is_returned_to_caller(container, make_agent, models):
    me = await make_agent("manager", model="tools")
    endless = scripted(lambda m, i: ModelResponse(parts=[ToolCallPart("list_files", {}, tool_call_id="l")]))
    models.models["helper"] = endless
    outputs = await run_tools(
        container, models, me.id, [("run_temp_agent", {"task": "x", "model": "helper"})]
    )

    limit = container.settings.runtime.temp_agent_request_limit
    assert f"did not finish within {limit} model requests" in outputs[0]
    assert list(container.settings.temp_dir.iterdir()) == []  # noqa: ASYNC240


async def test_workspace_removal_failure_keeps_answer_and_is_cleared_on_next_start(
    container, make_agent, models, monkeypatch, caplog
):
    import muliao.runtime.tools.temp_agent as temp_agent

    me = await make_agent("manager", model="tools")
    models.models["helper"] = scripted(lambda m, i: ModelResponse(parts=[TextPart("answer")]))

    def stuck(path):  # noqa: ANN001, ANN202
        raise OSError("Directory not empty")

    monkeypatch.setattr(temp_agent, "_remove", stuck)
    outputs = await run_tools(
        container, models, me.id, [("run_temp_agent", {"task": "x", "model": "helper"})]
    )
    monkeypatch.undo()

    assert outputs == ["answer"]
    assert any("will be cleared on next start" in r.getMessage() for r in caplog.records)
    assert len(list(container.settings.temp_dir.iterdir())) == 1  # noqa: ASYNC240
    temp_agent.clear_temp_dirs(container.settings.temp_dir)
    assert list(container.settings.temp_dir.iterdir()) == []  # noqa: ASYNC240


def test_unremovable_leftover_is_logged_and_does_not_block_startup(tmp_path, monkeypatch, caplog):
    import muliao.runtime.tools.temp_agent as temp_agent

    for name in ("temp-agent-a", "temp-agent-b"):
        (tmp_path / name).mkdir()
    real = temp_agent._remove

    def selective(path):  # noqa: ANN001, ANN202
        if path.name == "temp-agent-a":
            raise PermissionError("busy")
        real(path)

    monkeypatch.setattr(temp_agent, "_remove", selective)
    temp_agent.clear_temp_dirs(tmp_path)

    assert [p.name for p in tmp_path.iterdir()] == ["temp-agent-a"]
    assert any("not removable" in r.getMessage() for r in caplog.records)


async def test_agent_sees_only_its_tool_groups_plus_self_improvement(container, models):
    import pytest

    from muliao.agents import AgentSpec
    from muliao.errors import InvalidInput

    triage = await container.agents.create(
        AgentSpec(
            name="triage", description="handles webhooks", instructions="", model="m", tool_groups=["notify"]
        ),
        author="user",
    )
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, triage.id, "event", origin=Origin.TRIGGER)
    await drain(container)

    assert sorted(seen) == sorted(
        [
            "notify_user",
            "read_skill",
            "write_skill",
            "delete_skill",
            "edit_skill",
            "memory_append",
            "memory_replace",
            "instructions_replace",
        ]
    )
    with pytest.raises(InvalidInput, match="tool groups"):
        await container.agents.create(
            AgentSpec(name="x", description="d", instructions="", tool_groups=["root"]), author="user"
        )


async def test_restricted_agent_cannot_write_public_skills_or_use_mcp(container, models):
    from muliao.agents import AgentSpec

    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add(
        None, "echo", spec, probe=False, author="user"
    )  # 公共 MCP：受限 agent 也看不到它的工具
    triage = await container.agents.create(
        AgentSpec(name="triage", description="d", instructions="", model="m", tool_groups=["notify"]),
        author="user",
    )
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, triage.id, "event", origin=Origin.TRIGGER)
    await drain(container)

    assert "write_public_skill" not in seen and not any(name.startswith("echo_") for name in seen)
    assert "write_skill" in seen  # 私有 skill 仍可写


async def test_asked_agent_runs_with_its_own_tool_groups(container, make_agent, models):
    from muliao.agents import AgentSpec

    await container.agents.create(
        AgentSpec(name="narrow", description="d", instructions="", model="n", tool_groups=["notify"]),
        author="user",
    )
    seen: list[str] = []

    def narrow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("answer")])

    models.models["n"] = scripted(narrow)
    me = await make_agent("manager", model="tools")
    outputs = await run_tools(
        container, models, me.id, [("ask_agent", {"agent": "narrow", "message": "q", "thread": "t"})]
    )

    assert outputs == ["answer"]
    assert "run_shell" not in seen and "notify_user" in seen


def test_public_skills_group_can_edit_part_of_a_public_skill(container):
    [toolset] = container.consumer._tool_groups.groups["public_skills"].toolsets  # noqa: SLF001
    assert set(toolset.tools) == {"write_public_skill", "edit_public_skill", "delete_public_skill"}


async def test_trigger_create_tells_the_model_the_configured_precheck_time_limit(container):
    """agent 写检查脚本时要知道时限：生产上一个「正常 15 秒、网络抖动时重试 30~60 秒」的脚本反复超时。"""
    toolset = container.consumer._tool_groups.groups["triggers"].toolsets[0]  # noqa: SLF001
    create = toolset.tools["trigger_create"]
    limit = container.settings.runtime.precheck_timeout_seconds
    assert f"longer than {limit:g} seconds" in create.description
    # 参数说明仍来自 docstring
    assert "Optional command deciding whether to fire" in str(create.function_schema.json_schema)


async def test_run_shell_tells_the_model_its_configured_time_limits(container):
    run_shell = container.consumer._tool_groups.groups["workspace"].toolsets[0].tools["run_shell"]  # noqa: SLF001
    runtime = container.settings.runtime
    assert f"after {runtime.shell_default_timeout:g} seconds" in run_shell.description
    assert f"at most {runtime.shell_max_timeout:g}" in run_shell.description


async def test_run_shell_rejects_a_zero_timeout_instead_of_using_the_default(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    [output] = await run_tools(
        container, models, me.id, [("run_shell", {"command": "echo hi", "timeout": 0})]
    )
    assert "timeout must be in" in output


async def test_read_file_rejects_negative_offsets(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    [output] = await run_tools(container, models, me.id, [("read_file", {"path": "x.txt", "offset": -10})])
    assert "offset must be >= 0" in output
