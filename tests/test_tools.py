"""agent 能用工具完成的系统操作：管理 agent、MCP 启停、cron 修改、事件触发器、vault、thread 列表。"""

from collections.abc import Callable

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.profiles import ModelProfile

from muliao.agents.api import AgentPatch, AgentSpec, McpTransport, mcp_config
from muliao.errors import InvalidInput
from muliao.sessions.api import Origin
from tests.conftest import login, session_tools
from tests.scripted import scripted
from tests.test_flow import drain, start, tool_returns


async def run_tools(
    container, models, agent_id: str, calls: list[tuple[str, dict]], profile: ModelProfile | None = None
) -> list[str]:
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

    models.models["tools"] = scripted(fn, profile)
    await start(container, agent_id, "do it", origin=Origin.TRIGGER)
    await drain(container)
    return outputs


async def test_agent_creates_and_reconfigures_agents(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "create_agent",
                {"name": "researcher", "description": "finds facts"},
            ),
            ("update_agent", {"agent": "manager", "description": "Delegates research to researcher."}),
            ("get_agent", {"agent": "researcher"}),
            ("delete_agent", {"agent": "researcher"}),
        ],
    )

    assert outputs[0] == "created agent researcher"
    assert "finds facts" in outputs[2] and "fake:default" in outputs[2]  # 未指定模型时取服务端默认值
    assert outputs[3] == "deleted agent researcher"
    assert [a.name for a in await container.agents.list()] == ["manager"]
    assert (await container.agents.get(me.id)).description == "Delegates research to researcher."
    # 结果里只出现名称，不出现 id
    assert me.id not in "\n".join(outputs)


async def test_get_agent_shows_roles_resulting_tools_and_every_permission(container, make_agent, models):
    from muliao.agents.service import AgentSpec

    await container.agents.create(
        AgentSpec(name="narrow", description="d", model="n", deny=["issue:update"]),
        author="user",
    )
    me = await make_agent("manager", model="tools", roles=["admin"])
    [detail] = await run_tools(container, models, me.id, [("get_agent", {"agent": "narrow"})])

    assert "roles=['agent']" in detail and "deny=['issue:update']" in detail
    # 最终工具：角色的减去 deny；agent 角色没有写全局资源的工具；没勾选公共 MCP
    tools = detail.split("available_roles")[0]
    assert "'issue_create'" in tools and "'memory_append'" in tools
    assert "'issue_update'" not in tools and "'memory_append_public'" not in tools
    assert "mcp_servers=[]" in detail
    # 参照：全部角色与每个工具的权限 id、读写（没配置 [web] 时不列 web 工具）
    assert "name='admin'" in detail and "allow=['*']" in detail
    assert "tool='delete_agent', permission='agent:delete', access='write'" in detail
    assert "web_search" not in detail


def test_registered_tools_must_match_the_permission_table_both_ways():
    from muliao.agents.permissions import TEMP_AGENT_TOOLS, TOOL_PERMISSIONS
    from muliao.container import _check_permissions

    every = list(TOOL_PERMISSIONS)
    temp = list(TEMP_AGENT_TOOLS)
    no_web = [t for t in temp if not t.startswith("web_")]
    _check_permissions(every, temp, web_configured=True)
    _check_permissions([t for t in every if not t.startswith("web_")], no_web, web_configured=False)
    with pytest.raises(RuntimeError, match=r"unregistered \['web_fetch', 'web_search'\]"):
        _check_permissions([t for t in every if not t.startswith("web_")], temp, web_configured=True)
    # 没登记权限的工具谁也拿不到：启动即报
    with pytest.raises(RuntimeError, match=r"without permission \['new_tool'\]"):
        _check_permissions([*every, "new_tool"], temp, web_configured=True)
    with pytest.raises(RuntimeError, match=r"duplicated \['run_shell'\]"):
        _check_permissions([*every, "run_shell"], temp, web_configured=True)
    # 临时 agent 注册的工具与 TEMP_AGENT_TOOLS 不一致（界面列出的就不准）：启动即报
    with pytest.raises(RuntimeError, match=r"temp agent tools"):
        _check_permissions(every, no_web, web_configured=True)
    with pytest.raises(RuntimeError, match=r"temp agent tools"):
        _check_permissions(every, [*temp, "job_kill"], web_configured=True)


async def test_agent_pauses_trigger_and_reenables_mcp_server(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
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
    assert [m.name for m in container.mcp.index(me.id, ())[0]] == ["echo"]


async def test_agent_creates_webhook_but_only_the_user_can_issue_its_token(
    client, container, make_agent, models
):
    me = await make_agent("manager", model="tools", roles=["admin"])
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
    assert await container.vault.view(me.id) == []


async def test_agent_manages_private_and_public_secrets(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
    other = await make_agent("other")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("vault_set", {"key": "A", "value": "private-a", "description": "mine"}),
            ("vault_set_public", {"key": "B", "value": "public-b", "description": "shared"}),
            ("vault_delete", {"key": "A"}),
            ("vault_list", {}),
        ],
    )

    assert "private-a" not in "\n".join(outputs) and "public-b" not in "\n".join(outputs)
    assert await container.vault.resolve_many(other.id, ["B"]) == {"B": "public-b"}
    assert [s.key for s in await container.vault.visible_to(me.id)] == ["B"]


async def test_list_threads_shows_each_thread_with_its_state(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
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
    # 被问的 agent 已经交回结局：没有进行中的事，附上它的回答
    assert "review" in outputs[1] and "worker" in outputs[1] and "'idle'" in outputs[1]


async def test_temp_agent_runs_once_in_a_discarded_workspace_and_returns_its_answer(
    container, make_agent, models
):
    me = await make_agent("manager", model="tools", roles=["admin"])
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
    assert seen["tools"] == ["edit_file", "read_file", "run_shell", "share_file", "write_file"]
    assert "temporary helper agent working for agent 'manager'" in str(seen["instructions"])
    # 不留下会话
    assert len(await container.sessions.list(agent_id=None, origins=None, limit=10, offset=0)) == 1


@pytest.mark.parametrize(
    ("deny", "expected"),
    [
        ((), ["edit_file", "read_file", "run_shell", "share_file", "write_file"]),
        (("shell:run",), ["edit_file", "read_file", "share_file", "write_file"]),
    ],
)
async def test_temp_agent_only_gets_what_the_callers_permissions_allow(container, models, deny, expected):
    # 没配置 web、没勾选 MCP：临时 agent 只拿到文件与 shell；调用方禁了 shell:run 时就没有 shell
    me = await container.agents.create(
        AgentSpec(name="manager", description="d", model="tools", deny=deny), author="user"
    )
    seen: list[str] = []

    def helper(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(sorted(t.name for t in info.function_tools))
        return ModelResponse(parts=[TextPart("done")])

    models.models["helper"] = scripted(helper)
    await run_tools(container, models, me.id, [("run_temp_agent", {"task": "t", "model": "helper"})])

    assert seen == expected
    # 界面与 get_agent 列出的就是这一份（同一份规则算出）
    resolved = container.agents.resolve(await container.agents.rules(me)).temp_agent
    assert resolved is not None and sorted(resolved) == expected


async def test_get_agent_lists_the_tools_its_temp_agents_get(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
    await container.agents.create(AgentSpec(name="limited", description="d"), author="user")
    await container.agents.create(
        AgentSpec(name="plain", description="d", deny=["temp_agent:run"]), author="user"
    )
    outputs = await run_tools(
        container, models, me.id, [("get_agent", {"agent": "limited"}), ("get_agent", {"agent": "plain"})]
    )

    limited, plain = outputs
    assert "temp_agent_tools=['read_file', 'write_file', 'edit_file', 'share_file', 'run_shell']" in limited
    # 不能开临时 agent 时没有这一项
    assert "temp_agent_tools=None" in plain


async def test_parallel_partial_updates_of_one_agent_both_survive(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
    target = await make_agent("worker")

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "update_agent", {"agent": "worker", "description": "new desc"}, tool_call_id="a"
                    ),
                    ToolCallPart(
                        "update_agent", {"agent": "worker", "new_name": "worker2"}, tool_call_id="b"
                    ),
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    models.models["tools"] = scripted(fn)
    await start(container, me.id, "go", origin=Origin.TRIGGER)
    await drain(container)

    updated = await container.agents.get(target.id)
    assert (updated.description, updated.name) == ("new desc", "worker2")


async def test_tool_errors_name_agents_but_never_expose_ids(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
    other = await make_agent("worker")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("create_agent", {"name": "worker", "description": "d"}),
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

    from muliao.platform.tables import TaskStatus
    from muliao.sessions.lanes import RUN_TASK
    from tests.test_flow import tasks_of

    me = await make_agent("manager", model="tools", roles=["admin"])

    def broken(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=400, model_name="helper", body="bad request")

    models.models["helper"] = scripted(broken)
    await run_tools(container, models, me.id, [("run_temp_agent", {"task": "x", "model": "helper"})])

    assert list(container.settings.temp_dir.iterdir()) == []  # noqa: ASYNC240
    [turn] = await tasks_of(container, RUN_TASK)
    assert turn.status == TaskStatus.FAILED and "400" in turn.error


async def test_temp_agent_is_not_capped_by_pydantic_ai_default_request_limit(container, make_agent, models):
    """临时 agent 和长期 agent 一样不设请求数上限（#45）：越过 pydantic-ai 默认的 50 次请求照样做完。"""
    me = await make_agent("manager", model="tools", roles=["admin"])
    steps = 60

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if sum(isinstance(m, ModelResponse) for m in messages) >= steps:
            return ModelResponse(parts=[TextPart("finished")])
        return ModelResponse(parts=[ToolCallPart("write_file", {"path": "x.txt", "content": "x"})])

    models.models["helper"] = scripted(fn)
    outputs = await run_tools(
        container, models, me.id, [("run_temp_agent", {"task": "x", "model": "helper"})]
    )

    assert outputs == ["finished"]
    assert list(container.settings.temp_dir.iterdir()) == []  # noqa: ASYNC240


async def test_workspace_removal_failure_keeps_answer_and_is_cleared_on_next_start(
    container, make_agent, models, monkeypatch, caplog
):
    import muliao.agent_runtime.temp_agent as temp_agent

    me = await make_agent("manager", model="tools", roles=["admin"])
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
    import muliao.agent_runtime.temp_agent as temp_agent

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


async def test_agent_role_sees_every_tool_but_global_writes_and_no_unselected_mcp(container, models):
    from muliao.agents.service import AgentSpec

    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    # 公共 MCP：没勾选就看不到它的工具
    await container.mcp.add(None, "echo", spec, probe=False, author="user")
    triage = await container.agents.create(
        AgentSpec(name="triage", description="d", model="m"), author="user"
    )
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, triage.id, "event", origin=Origin.TRIGGER)
    await drain(container)

    assert sorted(seen) == sorted(container.agents.resolve(await container.agents.rules(triage)).tools)
    assert not {"write_public_skill", "memory_append_public", "vault_set_public", "create_agent"} & set(seen)
    assert {"write_skill", "memory_append", "vault_set", "run_shell", "trigger_create"} <= set(seen)
    assert not any(name.startswith("echo_") for name in seen)


async def test_asked_agent_runs_with_its_own_permissions(container, make_agent, models):
    from muliao.agents.service import AgentSpec

    await container.agents.create(
        AgentSpec(name="narrow", description="d", model="n", deny=["shell:*"]), author="user"
    )
    seen: list[str] = []

    def narrow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("answer")])

    models.models["n"] = scripted(narrow)
    me = await make_agent("manager", model="tools", roles=["admin"])
    outputs = await run_tools(
        container, models, me.id, [("ask_agent", {"agent": "narrow", "message": "q", "thread": "t"})]
    )

    assert outputs == ["answer"]
    # 被问的 agent 用自己的权限，不是调用方（admin）的
    assert "run_shell" not in seen and "notify_user" in seen and "create_agent" not in seen


@pytest.mark.parametrize(("tool", "public"), [("edit_skill", False), ("edit_public_skill", True)])
async def test_edit_skill_tools_change_description_body_or_both_and_reject_empty_edits(
    container, make_agent, models, tool, public
):
    me = await make_agent("alpha", model="tools", roles=["admin"])
    owner = None if public else me.id
    await container.skills.write(owner, "digest", "When digesting", "step one", me.id)

    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (tool, {"name": "digest", "description": "When summarizing"}),
            (tool, {"name": "digest", "old_text": "step one", "new_text": "step 1"}),
            (tool, {"name": "digest", "new_text": "step 2", "description": "When digesting"}),
            (tool, {"name": "digest"}),
        ],
    )

    scope = "public" if public else "private"
    assert outputs[:3] == [f"edited {scope} skill digest"] * 3
    assert "nothing to change" in outputs[3]
    skill = container.skills.read(owner, "digest")
    assert (skill.meta.description, skill.body) == ("When digesting", "step 1\n\nstep 2")


async def test_trigger_create_tells_the_model_the_configured_precheck_time_limit(container):
    """agent 写检查脚本时要知道时限：生产上一个「正常 15 秒、网络抖动时重试 30~60 秒」的脚本反复超时。"""
    create = session_tools(container)["trigger_create"]
    limit = container.settings.runtime.precheck_timeout_seconds
    assert f"longer than {limit:g} seconds" in create.description
    # 参数说明仍来自 docstring
    assert "Optional command deciding whether to fire" in str(create.function_schema.json_schema)


async def test_run_shell_tells_the_model_its_configured_limits(container):
    run_shell = session_tools(container)["run_shell"]
    runtime = container.settings.runtime
    assert f"after {runtime.shell_default_timeout:g} seconds" in run_shell.description
    assert f"at most {runtime.shell_max_timeout:g}" in run_shell.description
    assert (
        f"Only the first {runtime.tool_output_max_chars} characters of output are kept"
        in run_shell.description
    )


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


async def test_edit_file_replace_all_and_helpful_misses(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    content = "def f():\n    x = 1\n    return x\n\nx = 2\n"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "a.py", "content": content}),
            ("edit_file", {"path": "a.py", "old_text": "x", "new_text": "y"}),
            ("edit_file", {"path": "a.py", "old_text": "x = 1\nreturn x", "new_text": "z"}),
            ("edit_file", {"path": "a.py", "old_text": "    x = 1\n    return 0", "new_text": "z"}),
            ("edit_file", {"path": "a.py", "old_text": "x", "new_text": "y", "replace_all": True}),
            ("read_file", {"path": "a.py"}),
        ],
    )
    assert "at lines 2, 3, 5" in outputs[1]
    assert "lines 2-3 match it except for whitespace" in outputs[2]
    assert "first non-blank line is at line 2" in outputs[3]
    assert "3 replacements" in outputs[4]
    assert "y = 1" in outputs[5] and "return y" in outputs[5] and "y = 2" in outputs[5]


async def test_edit_file_applies_several_edits_all_or_nothing(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    content = "a = 1\nb = 2\nc = 3\n"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "m.py", "content": content}),
            (
                "edit_file",
                {
                    "path": "m.py",
                    "edits": [
                        {"old_text": "a = 1", "new_text": "a = 10"},
                        {"old_text": "missing", "new_text": "x"},
                    ],
                },
            ),
            ("read_file", {"path": "m.py"}),
            (
                "edit_file",
                {
                    "path": "m.py",
                    "edits": [
                        {"old_text": "a = 1", "new_text": "a = 10"},
                        {"old_text": "a = 10\nb", "new_text": "a = 10\nbb"},
                        {"old_text": " = ", "new_text": "=", "replace_all": True},
                    ],
                },
            ),
            ("read_file", {"path": "m.py"}),
            (
                "edit_file",
                {
                    "path": "m.py",
                    "old_text": "a",
                    "new_text": "z",
                    "edits": [{"old_text": "b", "new_text": "y"}],
                },
            ),
            ("edit_file", {"path": "m.py", "new_text": "z"}),
            ("edit_file", {"path": "m.py", "old_text": "a=10"}),
            ("edit_file", {"path": "m.py", "new_text": "", "edits": [{"old_text": "b", "new_text": "y"}]}),
            ("read_file", {"path": "m.py"}),
        ],
    )
    assert "edit 2 of 2" in outputs[1] and "1 earlier edit(s) applied" in outputs[1]
    assert "nothing was written" in outputs[1]
    assert "1\ta = 1\n2\tb = 2\n3\tc = 3" in outputs[2]
    assert "5 replacements" in outputs[3]
    assert "1\ta=10\n2\tbb=2\n3\tc=3" in outputs[4]
    assert "not both" in outputs[5]
    assert "old_text and new_text are required" in outputs[6]
    assert "old_text and new_text are required" in outputs[7]
    assert "not both" in outputs[8]
    assert "1\ta=10\n2\tbb=2\n3\tc=3" in outputs[9]


async def test_read_file_reads_several_parts_with_line_numbers_and_reports_bad_parts_inline(
    container, make_agent, models
):
    me = await make_agent("alpha", model="tools")
    content = "".join(f"line{i}\n" for i in range(1, 11))
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "a.txt", "content": content}),
            ("write_file", {"path": "b.txt", "content": "only\n"}),
            ("write_file", {"path": "empty.txt", "content": ""}),
            (
                "read_file",
                {
                    "reads": [
                        {"path": "a.txt", "offset": 1, "limit": 2},
                        {"path": "missing.txt"},
                        {"path": "a.txt", "offset": 8},
                        {"path": "b.txt"},
                        {"path": "a.txt", "offset": 50},
                        {"path": "empty.txt"},
                    ]
                },
            ),
            ("read_file", {"path": "a.txt", "reads": [{"path": "b.txt"}]}),
            ("read_file", {}),
        ],
    )
    assert "(0 lines, 0 chars)" in outputs[2]
    out = outputs[3]
    assert "a.txt lines 2-3 of 10 <==\n2\tline2\n3\tline3\n… 7 more lines" in out
    assert "missing.txt: file not found" in out
    assert "a.txt lines 9-10 of 10 <==\n9\tline9\n10\tline10" in out
    assert "b.txt lines 1-1 of 1 <==\n1\tonly" in out
    assert "==> a.txt: offset 50 is past the end" in out
    assert "empty.txt (empty file) <==" in out
    # 按给出的次序
    assert (
        out.index("lines 2-3") < out.index("missing.txt") < out.index("lines 9-10") < out.index("b.txt lines")
    )
    assert "not both" in outputs[4]
    assert "path is required" in outputs[5]


async def test_edit_file_shows_the_changed_lines_with_numbers_so_no_reread_is_needed(
    container, make_agent, models
):
    me = await make_agent("alpha", model="tools")
    content = "".join(f"l{i}\n" for i in range(1, 21))
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "e.txt", "content": content}),
            ("write_file", {"path": "e.txt", "content": content}),
            (
                "edit_file",
                {
                    "path": "e.txt",
                    "edits": [
                        {"old_text": "l3\n", "new_text": "l3\nnew\n"},
                        {"old_text": "l15\n", "new_text": ""},
                    ],
                },
            ),
            ("edit_file", {"path": "e.txt", "old_text": "l1\n", "new_text": "l1\n"}),
            ("write_file", {"path": "n.txt", "content": "a\nb"}),
            ("edit_file", {"path": "n.txt", "old_text": "b", "new_text": "b\n"}),
            ("edit_file", {"path": "n.txt", "old_text": "a\nb\n", "new_text": ""}),
        ],
    )
    assert outputs[0].startswith("created ") and "(20 lines, " in outputs[0]
    assert outputs[1].startswith("overwrote ")
    edited = outputs[2]
    assert "(2 replacements; now 20 lines)" in edited
    # 插入处：前后各 2 行上下文
    assert "lines 2-6:\n2\tl2\n3\tl3\n4\tnew\n5\tl4\n6\tl5" in edited
    # 删除处只剩上下文，行号是改后的
    assert "lines 14-17:\n14\tl13\n15\tl14\n16\tl16\n17\tl17" in edited
    assert "the text is unchanged" in outputs[3]
    assert "only line endings changed" in outputs[5]
    assert "now 0 lines); the file is now empty" in outputs[6]


async def test_read_file_truncates_very_long_lines(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "min.js", "content": "a" * 5000 + "\nok\n"}),
            ("read_file", {"path": "min.js"}),
        ],
    )
    assert "[line truncated]" in outputs[1] and "2\tok" in outputs[1] and len(outputs[1]) < 2100


async def test_read_file_returns_an_image_file_as_an_image_the_model_sees_and_stores_only_a_reference(
    container, make_agent, models, monkeypatch
):
    import base64

    from pydantic_ai.messages import BinaryContent, ImageUrl

    from muliao.agent_runtime import workspace_tools
    from muliao.files.api import parse_file_url
    from tests.test_flow import history_of

    # 上限按 provider 的 5MB（base64 编码后）换算
    assert len(base64.b64encode(b"x" * workspace_tools._MAX_IMAGE_BYTES)) <= 5 * 1024 * 1024
    me = await make_agent("alpha", model="tools")
    png = b"\x89PNG\r\n\x1a\n" + b"chart"
    workspace = container.agents.workspace(me.id)
    (workspace / "shot.png").write_bytes(png)
    (workspace / "big.jpg").write_bytes(b"x" * (len(png) + 1))
    monkeypatch.setattr(workspace_tools, "_MAX_IMAGE_BYTES", len(png))
    seen: list[object] = []
    calls = [
        ("read_file", {"path": "shot.png"}),
        ("read_file", {"path": "shot.png", "offset": 3}),
        ("read_file", {"reads": [{"path": "shot.png"}]}),
        ("read_file", {"path": "big.jpg"}),
        ("read_file", {"path": "gone.webp"}),
    ]

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        seen.extend(p.content for p in last.parts if p.part_kind in ("tool-return", "retry-prompt"))
        step = sum(isinstance(m, ModelResponse) for m in messages)
        if step < len(calls):
            name, args = calls[step]
            return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{step}")])
        return ModelResponse(parts=[TextPart("done")])

    models.models["tools"] = scripted(fn)
    session = await start(container, me.id, "look", origin=Origin.TRIGGER)
    await drain(container)

    # 模型在工具结果里看到图片本身
    caption, image = seen[0]
    assert caption.endswith("shot.png (image/png, 13 bytes)")
    assert image == BinaryContent(png, media_type="image/png")
    assert "offset/limit do not apply to an image file" in str(seen[1])
    assert "an image file cannot be read in reads" in str(seen[2])
    assert "image too large to view" in str(seen[3]) and "the limit is 13 bytes" in str(seen[3])
    assert "file not found" in str(seen[4])
    # 历史里只存文件引用（ToolImagesToFiles），内容在文件存储里
    [stored, *_] = [
        p for m in await history_of(container, session.id) for p in m.parts if p.part_kind == "tool-return"
    ]
    [_, ref] = stored.content
    assert isinstance(ref, ImageUrl)
    [meta] = await container.files.referenced([parse_file_url(ref.url)[0]])
    assert (meta.name, container.files.read(meta)) == ("read_file.png", png)


async def test_a_temp_agent_started_from_a_tool_shares_the_model_limit_without_deadlock(
    container, make_agent, models
):
    """模型请求按 provider 限流（agent_runtime.llm），会话运行与它在工具里开的临时 agent 用同一个 provider：上限为 1
    时也能跑完。工具在模型请求结束、配额还掉之后才执行；pydantic-ai 的嵌套检测只看正在进行的请求。升级 pydantic-ai
    后这一点若变了，这里会卡住或报 UserError。"""
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    from muliao.agent_runtime.llm import _ProviderLimiter

    me = await make_agent("manager", model="outer")
    limiter = _ProviderLimiter("p", 1)
    answers: list[str] = []

    def outer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if sum(isinstance(m, ModelResponse) for m in messages) == 0:
            return ModelResponse(
                parts=[ToolCallPart("run_temp_agent", {"task": "x", "model": "helper"}, tool_call_id="t")]
            )
        answers.extend(str(p.content) for p in tool_returns(messages))
        return ModelResponse(parts=[TextPart("done")])

    models.models["outer"] = ConcurrencyLimitedModel(scripted(outer), limiter)
    models.models["helper"] = ConcurrencyLimitedModel(
        scripted(lambda _m, _i: ModelResponse(parts=[TextPart("helped")])), limiter
    )
    await start(container, me.id, "do it", origin=Origin.TRIGGER)
    await drain(container, limit_seconds=5)

    assert answers == ["helped"]


async def test_a_corrupt_public_skill_is_listed_as_corrupt_in_the_index_and_the_run_goes_on(
    container, make_agent, models
):
    alpha = (await make_agent("alpha", model="m")).id
    await container.skills.write(None, "good", "a good skill", "x", "user")
    broken = container.resources.owner_dir(None) / "skills" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [oops\n---\nbody")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    index = seen[0][seen[0].index("## Skill index") :]
    assert "- `good` (public): a good skill" in index
    assert "- public skill 'broken' is corrupt and cannot be used until it is rewritten or deleted:" in index


THINKS = ModelProfile(supports_thinking=True)


def _thinking_seen(seen: list[object]) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # pydantic_ai 把 ModelSettings.thinking 移到请求参数上再交给 provider 换算
        seen.append(info.model_request_parameters.thinking)
        return ModelResponse(parts=[TextPart("ok")])

    return fn


async def test_thinking_level_reaches_the_model_and_temp_agents_inherit_it(container, make_agent, models):
    """临时 agent 不指定时沿用调用方的档位；"default" 什么都不传；模型不支持的档位如实拒绝。"""
    models.models["tools"] = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("x")]), THINKS)
    me = await make_agent("manager", model="tools", roles=["admin"])
    await container.agents.update(me.id, AgentPatch(thinking="high"))
    seen: list[object] = []
    models.models["helper"] = scripted(_thinking_seen(seen), THINKS)
    models.models["plain"] = scripted(_thinking_seen(seen))
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("run_temp_agent", {"task": "x", "model": "helper"}),
            ("run_temp_agent", {"task": "x", "model": "helper", "thinking": "default"}),
            ("run_temp_agent", {"task": "x", "model": "helper", "thinking": "off"}),
            ("run_temp_agent", {"task": "x", "model": "plain"}),
            ("update_agent", {"agent": "manager", "thinking": "low"}),
            ("get_agent", {"agent": "manager"}),
            ("update_agent", {"agent": "manager", "thinking": "max"}),
        ],
        THINKS,
    )

    assert seen == ["high", None, False]
    assert "'plain' does not take a thinking setting" in outputs[3]
    assert "thinking='low'" in outputs[5]
    assert "'xhigh'" in outputs[6]  # 不在档位里：交还模型重试
    assert (await container.agents.get(me.id)).thinking == "low"


async def test_agent_run_sends_its_thinking_level(container, make_agent, models):
    seen: list[object] = []
    models.models["m"] = scripted(_thinking_seen(seen), THINKS)
    default = await make_agent("plain", model="m")
    thinker = await make_agent("thinker", model="m")
    await container.agents.update(thinker.id, AgentPatch(thinking="xhigh"))
    for agent in (default, thinker):
        await start(container, agent.id, "q", origin=Origin.TRIGGER)
        await drain(container)

    assert seen == [None, "xhigh"]


async def test_thinking_the_model_would_silently_drop_is_rejected(container, make_agent, models):
    """pydantic_ai 对 profile 不支持的设置只是丢掉；平台在写入时就拒绝，不让设置看似生效。"""
    models.models["always"] = scripted(
        lambda _m, _i: ModelResponse(parts=[]), ModelProfile(thinking_always_enabled=True)
    )
    me = await make_agent("a", model="always")
    with pytest.raises(InvalidInput, match="always thinks"):
        await container.agents.update(me.id, AgentPatch(thinking="off"))
    await container.agents.update(me.id, AgentPatch(thinking="medium"))
    with pytest.raises(InvalidInput, match="does not take a thinking setting"):
        await container.agents.update(me.id, AgentPatch(model="no-thinking"))
    assert (await container.agents.get(me.id)).model == "always"
