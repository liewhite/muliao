"""agent 能用工具完成的系统操作：管理 agent、MCP 启停、cron 修改、事件触发器、vault、thread 列表。"""

import json
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

from muliao.agents.api import AgentPatch, McpTransport, StoredKind, mcp_config
from muliao.errors import InvalidInput
from muliao.sessions.api import Origin
from tests.conftest import login, session_tools, share
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
        AgentSpec(name="narrow", description="d", model="n", deny=["use:tool:issue_update"]),
        actor="user",
    )
    narrow = await container.agents.find_by_name("narrow")
    await container.skills.write("notes", "d", "b", actor=narrow.id, owner=narrow.id)
    me = await make_agent("manager", model="tools", roles=["admin"])
    [output] = await run_tools(container, models, me.id, [("get_agent", {"agent": "narrow"})])

    # 模型收到的是一段 JSON 对象（agent_runtime.tool_results），不是 Python repr
    detail = json.loads(output)
    assert detail["roles"] == [] and detail["deny"] == ["use:tool:issue_update"]
    # 最终工具：base 的减去 deny；base 没有管理类工具
    assert {"issue_create", "memory_edit"} <= set(detail["tools"])
    assert not {"issue_update", "memory_create"} & set(detail["tools"])
    assert detail["items"] == ["memory:global", "memory:narrow", "skill:notes"]
    # 参照：全部角色（含固定的规则）与全部内置工具（没配置 [web] 时不列 web 工具）
    admin = next(r for r in detail["available_roles"] if r["name"] == "admin")
    assert admin["fixed_allow"] == ["use:tool:*", "write:*"]
    assert "delete_role" in detail["all_tools"] and "web_search" not in detail["all_tools"]


def test_registered_tools_must_match_the_permission_table_both_ways():
    from muliao.agents.permissions import TOOLS
    from muliao.container import _check_permissions

    every = list(TOOLS)
    _check_permissions(every, web_configured=True)
    _check_permissions([t for t in every if not t.startswith("web_")], web_configured=False)
    with pytest.raises(RuntimeError, match=r"unregistered \['web_fetch', 'web_search'\]"):
        _check_permissions([t for t in every if not t.startswith("web_")], web_configured=True)
    # 没登记权限的工具谁也拿不到：启动即报
    with pytest.raises(RuntimeError, match=r"without permission \['new_tool'\]"):
        _check_permissions([*every, "new_tool"], web_configured=True)
    with pytest.raises(RuntimeError, match=r"duplicated \['run_shell'\]"):
        _check_permissions([*every, "run_shell"], web_configured=True)


async def test_agent_pauses_trigger_and_lists_its_mcp_servers(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
    await container.triggers.create(
        name="daily", agent_id=me.id, kind="schedule", prompt="report", config={"cron": "0 9 * * *"}
    )
    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add("echo", spec, actor="user", owner=me.id, probe=False)
    # 停用的 server 不会被连接；agent 在 mcp_list 里看得到它（启停只在界面上）
    await container.mcp.set_enabled("echo", False, "user")

    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("trigger_update", {"name": "daily", "enabled": False}),
            ("mcp_list", {}),
        ],
    )

    assert outputs[0].startswith("updated trigger daily (paused)")
    assert not (await container.triggers.get_by_name("daily")).enabled
    [echo] = json.loads(outputs[1])
    assert (echo["name"], echo["yours"], echo["enabled"]) == ("echo", True, False)
    assert await container.mcp.index(me.id) == []


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
    assert "http://test/api/hooks/github" in outputs[1] and '"has_token":false' in outputs[1]
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
    assert await container.vault.usable_by(me.id) == []


async def test_agent_manages_its_secrets_and_shares_them_with_a_role(container, make_agent, models):
    me = await make_agent("manager", model="tools", roles=["admin"])
    other = await make_agent("other")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("vault_set", {"key": "A", "value": "private-a", "description": "mine"}),
            ("vault_set", {"key": "B", "value": "shared-b", "description": "shared"}),
            ("vault_delete", {"key": "A"}),
            ("create_role", {"name": "b-user", "description": "uses B", "allow": ["use:secret:B"]}),
            ("update_agent", {"agent": "other", "roles": ["b-user"]}),
            ("vault_list", {}),
        ],
    )

    assert "private-a" not in "\n".join(outputs) and "shared-b" not in "\n".join(outputs)
    assert json.loads(outputs[5]) == [{"key": "B", "yours": True, "description": "shared"}]
    assert await container.vault.resolve_many(other.id, ["B"]) == {"B": "shared-b"}
    assert [(s.key, s.owner) for s in await container.vault.usable_by(me.id)] == [("B", me.id)]


async def test_eth_sign_signs_with_a_vault_key_without_showing_it(container, make_agent, models, caplog):
    from eth_account import Account
    from eth_account.messages import encode_defunct

    key = "0x" + "11" * 32
    me = await make_agent("signer", model="tools", roles=["admin"])
    other = await make_agent("other")
    await container.vault.set("WALLET", key, "wallet", actor=other.id, owner=other.id)
    await container.vault.set("MINE", key, "wallet", actor=me.id, owner=me.id)
    caplog.set_level("INFO", logger="muliao.agents.secret_tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("eth_sign", {"key": "MINE", "kind": "message", "payload": "hello"}),
            # 别人的、没分给我的 key 用不了
            ("eth_sign", {"key": "WALLET", "kind": "message", "payload": "hello"}),
        ],
    )

    signed = json.loads(outputs[0])
    address = Account.from_key(key).address
    assert signed["signer"] == address
    assert Account.recover_message(encode_defunct(text="hello"), signature=signed["signature"]) == address
    assert "not found among the secrets you can use" in outputs[1]
    assert key.removeprefix("0x") not in "\n".join(outputs) + caplog.text
    [audit] = [r for r in caplog.records if r.getMessage() == "eth signed"]
    assert (audit.agent, audit.key, audit.sign_kind, audit.signer, audit.digest) == (
        me.id,
        "MINE",
        "message",
        address,
        signed["message_hash"],
    )


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
    [thread] = json.loads(outputs[1])
    assert (thread["thread"], thread["agent"], thread["state"], thread["answer"]) == (
        "review",
        "worker",
        "idle",
        "ok",
    )


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


async def test_agent_without_admin_can_only_change_its_own_description_model_and_thinking(
    container, make_agent, models
):
    me = await make_agent("worker", model="tools")
    other = await make_agent("other")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "update_agent",
                {"agent": "worker", "description": "new desc", "model": "tools", "thinking": "default"},
            ),
            ("update_agent", {"agent": "other", "description": "hijacked"}),
            ("update_agent", {"agent": "worker", "roles": ["admin"]}),
            ("update_agent", {"agent": "worker", "allow": ["use:tool:*"], "description": "x"}),
            ("update_agent", {"agent": "worker", "description": "y", "archived": True}),
            ("update_agent", {"agent": "worker", "new_name": "boss"}),
        ],
    )

    assert outputs[0] == "updated agent worker"
    assert "only an admin can change another agent" in outputs[1]
    assert "not roles" in outputs[2] and "not allow" in outputs[3] and "not archived" in outputs[4]
    assert "not name" in outputs[5]
    # 被拒的调用什么都没改：没提权、没归档，描述停在第一次成功的修改
    mine = await container.agents.get(me.id)
    assert (mine.name, mine.description, mine.archived) == ("worker", "new desc", False)
    assert (await container.agents.get(other.id)).description != "hijacked"
    _, rules = await container.agents.find_with_rules("worker")
    assert (rules.roles, rules.allow) == ((), ())


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


async def test_base_sees_every_tool_but_admin_ones_and_no_undistributed_mcp(container, models):
    from muliao.agents.service import AgentSpec

    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    # 归平台的 MCP：没分给它就看不到它的工具
    await container.mcp.add("echo", spec, actor="user", owner=None, probe=False)
    triage = await container.agents.create(AgentSpec(name="triage", description="d", model="m"), actor="user")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, triage.id, "event", origin=Origin.TRIGGER)
    await drain(container)

    assert sorted(seen) == sorted((await container.distribution.reach(triage.id)).tool_names)
    assert not {"memory_create", "create_agent", "create_role"} & set(seen)
    assert {"write_skill", "memory_edit", "vault_set", "run_shell", "trigger_create"} <= set(seen)
    assert not any(name.startswith("echo_") for name in seen)


async def test_asked_agent_runs_with_its_own_permissions(container, make_agent, models):
    from muliao.agents.service import AgentSpec

    await container.agents.create(
        AgentSpec(name="narrow", description="d", model="n", deny=["use:tool:run_shell"]), actor="user"
    )
    seen: list[str] = []

    def narrow(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("answer")])

    models.models["n"] = scripted(narrow)
    me = await make_agent("manager", model="tools", roles=["admin"])
    outputs = await run_tools(
        container,
        models,
        me.id,
        [("ask_agent", {"agent": "narrow", "message": "q", "thread": "t"})],
    )

    assert outputs == ["answer"]
    # 被问的 agent 用自己的权限，不是调用方（admin）的
    assert "run_shell" not in seen and "notify_user" in seen and "create_agent" not in seen


@pytest.mark.parametrize("platform_owned", [False, True])
async def test_edit_skill_changes_description_body_or_both_and_rejects_empty_edits(
    container, make_agent, models, platform_owned
):
    # admin 改得了归平台的
    me = await make_agent("alpha", model="tools", roles=["admin"])
    owner = None if platform_owned else me.id
    await container.skills.write("digest", "When digesting", "step one", actor="user", owner=owner)
    tool = "edit_skill"

    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (tool, {"name": "digest", "reason": "r1", "description": "When summarizing"}),
            (tool, {"name": "digest", "reason": "r2", "old_text": "step one", "new_text": "step 1"}),
            (tool, {"name": "digest", "reason": "r3", "new_text": "step 2", "description": "When digesting"}),
            (tool, {"name": "digest", "reason": "r4"}),
            (tool, {"name": "digest", "reason": "  ", "new_text": "step 3"}),
        ],
    )

    assert outputs[:3] == ["edited skill digest"] * 3
    assert "nothing to change" in outputs[3]
    assert "reason must not be blank" in outputs[4]
    skill = await container.skills.read("digest")
    assert (skill.meta.description, skill.body) == ("When digesting", "step 1\n\nstep 2")
    # 每一版记下改的原因，按新到旧；用户在界面上写的那一版仍是默认的动作描述
    history = await container.catalog.history(StoredKind.SKILL, "digest", 10)
    assert [r.message for r in history] == ["r3", "r2", "r1", "write skill digest"]


async def test_skill_and_memory_tools_record_the_reason_and_reject_a_blank_one(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "write_skill",
                {"name": "digest", "description": "d", "content": "b", "reason": "done it twice"},
            ),
            ("delete_skill", {"name": "digest", "reason": "merged into report"}),
            ("memory_edit", {"name": "alpha", "new_text": "- fact", "reason": " "}),
        ],
    )

    assert "reason must not be blank" in outputs[2]
    assert (await container.memory.read("alpha")).content == ""
    history = await container.catalog.history(StoredKind.SKILL, "digest", 10)
    # 删除那一版（content 为空）也带着原因
    assert [(r.message, r.after is None) for r in history] == [
        ("merged into report", True),
        ("done it twice", False),
    ]


async def test_memory_edit_and_edit_skill_apply_a_list_of_edits_as_one_version_or_not_at_all(
    container, make_agent, models
):
    # #375：要改多处时模型照 edit_file 传 `edits`；一次调用一版，任何一处对不上就都不写
    me = await make_agent("alpha", model="tools")
    await container.memory.put("alpha", "- a\n\n- b", "", actor="user")
    await container.skills.write("digest", "d", "step one\n\nstep two", actor=me.id, owner=me.id)
    two_and_append = [
        {"old_text": "- a", "new_text": "- A"},
        {"old_text": "- A", "new_text": "- AA"},  # 在前一处改过的结果上找
        {"new_text": "- c"},
    ]
    bad_second = [{"old_text": "- b", "new_text": "- B"}, {"old_text": "- missing", "new_text": "x"}]
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("memory_edit", {"name": "alpha", "reason": "r1", "edits": two_and_append}),
            ("memory_edit", {"name": "alpha", "reason": "r2", "edits": bad_second}),
            ("memory_edit", {"name": "alpha", "reason": "r3", "new_text": "- d", "edits": bad_second}),
            ("memory_edit", {"name": "alpha", "reason": "r4"}),
            (
                "edit_skill",
                {
                    "name": "digest",
                    "reason": "s1",
                    "description": "d2",
                    "edits": [
                        {"old_text": "one", "new_text": "1"},
                        {"old_text": "two", "new_text": "2"},
                    ],
                },
            ),
            (
                "edit_skill",
                {
                    "name": "digest",
                    "reason": "s2",
                    "edits": [{"old_text": "step 1", "new_text": "x"}, {"old_text": "nope", "new_text": "x"}],
                },
            ),
        ],
    )

    assert outputs[0].startswith("updated memory alpha")
    assert "edit 2 of 2" in outputs[1] and "nothing was written" in outputs[1] and "- AA" in outputs[1]
    assert "not both" in outputs[2]
    assert "nothing to change" in outputs[3]
    assert outputs[4] == "edited skill digest" and "edit 2 of 2" in outputs[5]
    assert (await container.memory.read("alpha")).content == "- AA\n\n- b\n\n- c"
    memory_history = await container.catalog.history(StoredKind.MEMORY, "alpha", 10)
    # 建 agent、put 各一版，三处修改只多一版
    assert (len(memory_history), memory_history[0].message) == (3, "r1")
    assert memory_history[1].after == "- a\n\n- b"
    skill = await container.skills.read("digest")
    assert (skill.meta.description, skill.body.strip()) == ("d2", "step 1\n\nstep 2")
    history = await container.catalog.history(StoredKind.SKILL, "digest", 10)
    assert [r.message for r in history][:2] == ["s1", "write skill digest"]


async def test_edit_skill_edits_and_creates_attachments_by_file_as_one_version_or_not_at_all(
    container, make_agent, models
):
    # #494：改一个附件不用再把全部附件拷出来、整个目录传回去
    from muliao.agents.skill_package import SkillFile

    me = await make_agent("alpha", model="tools")
    await container.skills.write(
        "digest",
        "d",
        "run the script",
        actor=me.id,
        owner=me.id,
        attachments={
            "scripts/run.sh": SkillFile("#!/bin/sh\necho one\n", executable=True),
            "notes.md": SkillFile("keep me\n"),
        },
    )
    tool = "edit_skill"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            # 单处：顶层 file
            (
                tool,
                {
                    "name": "digest",
                    "reason": "r1",
                    "file": "scripts/run.sh",
                    "old_text": "one",
                    "new_text": "1",
                },
            ),
            # 多文件一次一版：正文、已有附件、新建两个附件（#! 开头的可执行）
            (
                tool,
                {
                    "name": "digest",
                    "reason": "r2",
                    "edits": [
                        {"old_text": "the script", "new_text": "scripts/run.sh"},
                        {"file": "scripts/run.sh", "new_text": "echo 2"},
                        {"file": "scripts/new.py", "new_text": "#!/usr/bin/env python3\nprint(3)"},
                        {"file": "refs/api.md", "new_text": "# API"},
                    ],
                },
            ),
            # 第二处失败：什么都不写
            (
                tool,
                {
                    "name": "digest",
                    "reason": "r3",
                    "edits": [
                        {"file": "notes.md", "old_text": "keep me", "new_text": "changed"},
                        {"file": "scripts/run.sh", "old_text": "missing", "new_text": "x"},
                    ],
                },
            ),
            (tool, {"name": "digest", "reason": "r4", "file": "nope.md", "old_text": "a", "new_text": "b"}),
            (tool, {"name": "digest", "reason": "r5", "file": "../escape.md", "new_text": "x"}),
            (tool, {"name": "digest", "reason": "r6", "file": "/abs.md", "new_text": "x"}),
            (tool, {"name": "digest", "reason": "r7", "file": "tool.bin", "new_text": "x"}),
            (
                tool,
                {"name": "digest", "reason": "r8", "file": "big.md", "new_text": "x" * (256 * 1024 + 1)},
            ),
            (
                tool,
                {
                    "name": "digest",
                    "reason": "r9",
                    "file": "notes.md",
                    "new_text": "x",
                    "skill_dir": ".",
                },
            ),
        ],
    )

    assert outputs[:2] == ["edited skill digest"] * 2
    assert "edit 2 of 2" in outputs[2] and "nothing was written" in outputs[2]
    assert "nope.md of skill digest does not exist" in outputs[3] and "notes.md" in outputs[3]
    assert "'..' segment" in outputs[4] and "absolute" in outputs[5]
    assert "cannot be an attachment" in outputs[6] and "larger than" in outputs[7]
    assert "skill_dir replaces all attachments" in outputs[8]
    files = await container.skills.files("digest")
    assert files["scripts/run.sh"] == SkillFile("#!/bin/sh\necho 1\necho 2\n", executable=True)
    assert files["scripts/new.py"] == SkillFile("#!/usr/bin/env python3\nprint(3)\n", executable=True)
    assert files["refs/api.md"] == SkillFile("# API\n")
    assert files["notes.md"] == SkillFile("keep me\n")
    assert (await container.skills.read("digest")).body == "run scripts/run.sh"
    history = await container.catalog.history(StoredKind.SKILL, "digest", 10)
    assert [r.message for r in history] == ["r2", "r1", "write skill digest"]
    # 生成目录同步更新，可执行位按上面
    generated = container.skills.dir("digest")
    assert (generated / "scripts/new.py").stat().st_mode & 0o111
    assert not (generated / "refs/api.md").stat().st_mode & 0o111


async def test_edit_skill_keeps_modes_appends_code_as_is_and_rejects_colliding_paths(
    container, make_agent, models
):
    from muliao.agents.skill_package import SkillFile

    me = await make_agent("alpha", model="tools")
    await container.skills.write(
        "digest",
        "d",
        "body",
        actor=me.id,
        owner=me.id,
        attachments={
            "plain.py": SkillFile("x = 1\n"),
            "run.sh": SkillFile("#!/bin/sh\necho hi\n", executable=True),
            "scripts/a.py": SkillFile("def f():\n    pass\n"),
        },
    )
    tool = "edit_skill"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                tool,
                {
                    "name": "digest",
                    "reason": "r1",
                    "edits": [
                        {
                            "file": "plain.py",
                            "old_text": "x = 1",
                            "new_text": "#!/usr/bin/env python3\nx = 1",
                        },
                        {"file": "run.sh", "old_text": "#!/bin/sh\n", "new_text": ""},
                        {"file": "scripts/a.py", "new_text": "    return 1\n"},
                    ],
                },
            ),
            (tool, {"name": "digest", "reason": "r2", "file": "skill.md", "new_text": "x"}),
            (tool, {"name": "digest", "reason": "r3", "file": "Scripts/A.py", "new_text": "x"}),
            (tool, {"name": "digest", "reason": "r4", "file": "plain.py/b.py", "new_text": "x"}),
        ],
    )

    assert outputs[0] == "edited skill digest"
    assert "same file" in outputs[1] and "SKILL.md is the instructions" in outputs[1]
    assert "same file" in outputs[2] and "cannot also be a directory" in outputs[3]
    files = await container.skills.files("digest")
    # 已有文件保留原来的可执行位，不看改后是否以 #! 开头
    assert files["plain.py"] == SkillFile("#!/usr/bin/env python3\nx = 1\n", executable=False)
    assert files["run.sh"] == SkillFile("echo hi\n", executable=True)
    # 附件追加原样拼接：缩进不丢、不插空行
    assert files["scripts/a.py"].content == "def f():\n    pass\n    return 1\n"
    assert set(files) == {"SKILL.md", "plain.py", "run.sh", "scripts/a.py"}


async def test_a_skill_drafted_as_a_directory_is_written_whole_with_its_skill_md_as_instructions(
    container, make_agent, models
):
    # #493：草稿在 workspace 里是一个目录（SKILL.md + 附件），不用把正文内联进 content，也不会先存个空壳
    from muliao.agents.skill_package import SkillFile

    me = await make_agent("alpha", model="tools")
    draft = container.settings.workspaces_dir / me.id / "draft"
    (draft / "scripts").mkdir(parents=True)
    (draft / "SKILL.md").write_text("---\nname: digest\ndescription: When digesting\n---\n\nrun it\n")
    (draft / "scripts" / "run.py").write_text("print(1)\n")
    bare = draft.parent / "bare"
    bare.mkdir()
    (bare / "SKILL.md").write_text("just the body\n")
    (bare / "notes.md").write_text("n\n")
    tool = "write_skill"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (tool, {"name": "digest", "reason": "r1", "skill_dir": "draft"}),
            (tool, {"name": "digest", "reason": "r2", "skill_dir": "draft", "content": "x"}),
            (tool, {"name": "digest", "reason": "r3", "skill_dir": "draft", "description": "d"}),
            (tool, {"name": "other", "reason": "r4", "skill_dir": "draft"}),
            (tool, {"name": "lone", "reason": "r5", "skill_dir": "bare"}),
            (tool, {"name": "lone", "reason": "r6", "description": "d"}),
            ("edit_skill", {"name": "digest", "reason": "r7", "skill_dir": "bare"}),
            ("edit_skill", {"name": "digest", "reason": "r8", "skill_dir": "bare", "new_text": "more"}),
        ],
    )

    assert outputs[0].startswith("saved skill digest")
    assert "give the instructions once" in outputs[1] and "give the description once" in outputs[2]
    assert "does not match 'other'" in outputs[3]
    assert "no description" in outputs[4] and "no instructions" in outputs[5]
    assert outputs[6].startswith("edited skill digest")
    assert "removed attachments not in skill_dir: scripts/run.py" in outputs[6]
    assert "replaces the instructions" in outputs[7]
    # 编辑：目录里的 SKILL.md 换掉正文（没写描述就保留原描述），其余文件整体换掉附件
    skill = await container.skills.read("digest")
    assert (skill.meta.description, skill.body.strip()) == ("When digesting", "just the body")
    files = await container.skills.files("digest")
    assert set(files) == {"SKILL.md", "notes.md"} and files["notes.md"] == SkillFile("n\n")
    history = await container.catalog.history(StoredKind.SKILL, "digest", 10)
    assert [r.message for r in history] == ["r7", "r1"]


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
    assert "you must pass `timeout`" in run_shell.description and "no upper limit" in run_shell.description
    assert "every `heartbeat_seconds`" in run_shell.description
    assert "at most" not in run_shell.description
    assert (
        f"Only the first {runtime.tool_output_max_chars} characters of output are kept"
        in run_shell.description
    )


async def test_run_shell_requires_a_timeout_in_the_foreground_and_a_heartbeat_only_in_the_background(
    container, make_agent, models
):
    me = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("run_shell", {"command": "echo hi"}),
            ("run_shell", {"command": "echo hi", "timeout": 5, "heartbeat_seconds": 10}),
            ("run_shell", {"command": "true", "background": True, "heartbeat_seconds": 0}),
        ],
    )
    assert "timeout is required for a foreground command" in outputs[0]
    assert "heartbeat_seconds is only for background jobs" in outputs[1]
    assert "heartbeat_seconds must be greater than 0" in outputs[2]


async def test_run_shell_rejects_a_zero_timeout(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [("run_shell", {"command": "echo hi", "timeout": t}) for t in (0, float("nan"))],
    )
    assert len(outputs) == 2
    assert all("timeout must be greater than 0" in o for o in outputs)


async def test_run_shell_accepts_a_timeout_of_any_length(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    [output] = await run_tools(
        container, models, me.id, [("run_shell", {"command": "echo hi", "timeout": 3600})]
    )
    assert output.startswith("exit_code: 0")


async def test_read_file_offsets_are_1_based_line_numbers(container, make_agent, models):
    """offset 与输出的行号同为 1 起算（cat -n）：0 起算的习惯与负数都拒绝并说明，不悄悄返回错位的内容。"""
    me = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "x.txt", "content": "a\nb\nc\n"}),
            ("read_file", {"path": "x.txt", "offset": 2}),
            ("read_file", {"path": "x.txt", "offset": 0}),
            ("read_file", {"path": "x.txt", "offset": -10}),
        ],
    )
    assert outputs[1] == "     2\tb\n     3\tc"
    assert all("offset is a 1-based line number (offset=1 is the first line)" in o for o in outputs[2:])


async def test_file_tools_wait_for_earlier_calls_of_the_same_reply(container, make_agent, models):
    """#224：一次回复里先 run_shell 生成文件、再 read_file 它，read_file 要等 shell 做完，不能并行抢先报 file not found。"""
    me = await make_agent("alpha", model="tools")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "run_shell", {"command": "sleep 0.5; echo hi > made.txt", "timeout": 30}, "c0"
                    ),
                    ToolCallPart("read_file", {"path": "made.txt"}, "c1"),
                ]
            )
        seen.extend(str(p.content) for p in tool_returns(messages))
        seen.extend(str(p.content) for p in messages[-1].parts if isinstance(p, RetryPromptPart))
        return ModelResponse(parts=[TextPart("done")])

    models.models["tools"] = scripted(fn)
    await start(container, me.id, "do it", origin=Origin.TRIGGER)
    await drain(container)

    assert any("     1\thi" in s for s in seen), seen


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
    assert "edit 2 of 2" in outputs[1] and "1 earlier edit(s) to it applied" in outputs[1]
    assert "nothing was written" in outputs[1]
    assert "     1\ta = 1\n     2\tb = 2\n     3\tc = 3" in outputs[2]
    assert "5 replacements" in outputs[3]
    assert "     1\ta=10\n     2\tbb=2\n     3\tc=3" in outputs[4]
    assert "not both" in outputs[5]
    assert "locate the text in exactly one way" in outputs[6]
    assert "new_text is required" in outputs[7]
    assert "not both" in outputs[8]
    assert "     1\ta=10\n     2\tbb=2\n     3\tc=3" in outputs[9]


async def test_edit_file_changes_several_files_in_one_call_all_or_nothing(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "a.py", "content": "A = 1\n"}),
            ("write_file", {"path": "sub/b.py", "content": "B = 1\n"}),
            # 第 2 个文件对不上：两个都不写
            (
                "edit_file",
                {
                    "edits": [
                        {"path": "a.py", "old_text": "A = 1", "new_text": "A = 2"},
                        {"path": "sub/b.py", "old_text": "missing", "new_text": "x"},
                    ]
                },
            ),
            ("read_file", {"reads": [{"path": "a.py"}, {"path": "sub/b.py"}]}),
            # 顶层 path 是没写 path 的项的文件；同一文件换种写法也认成一份，后一项看得到前一项的结果
            (
                "edit_file",
                {
                    "path": "a.py",
                    "edits": [
                        {"old_text": "A = 1", "new_text": "A = 2"},
                        {"path": "sub/b.py", "old_text": "B = 1", "new_text": "B = 2"},
                        {"path": "sub/../a.py", "old_text": "A = 2", "new_text": "A = 3"},
                    ],
                },
            ),
            ("read_file", {"reads": [{"path": "a.py"}, {"path": "sub/b.py"}]}),
            ("edit_file", {"edits": [{"old_text": "A", "new_text": "Z"}]}),
            ("edit_file", {"edits": [{"path": "nope.py", "old_text": "A", "new_text": "Z"}]}),
            ("edit_file", {"old_text": "A", "new_text": "Z"}),
        ],
    )
    assert "edit 2 of 2" in outputs[2] and "b.py" in outputs[2] and "nothing was written" in outputs[2]
    assert "     1\tA = 1" in outputs[3] and "     1\tB = 1" in outputs[3]
    report = outputs[4]
    assert (
        "a.py (2 replacements; now 1 lines)" in report and "sub/b.py (1 replacement; now 1 lines)" in report
    )
    assert "sub/../a.py" not in report
    assert "     1\tA = 3" in outputs[5] and "     1\tB = 2" in outputs[5]
    assert "edit 1 has no path" in outputs[6]
    assert "file not found" in outputs[7] and "nope.py" in outputs[7]
    assert "path is required" in outputs[8]


async def test_edit_file_refuses_a_read_only_file_before_writing_any(
    container, make_agent, models, monkeypatch
):
    import muliao.workspace.file_tools as wt

    me = await make_agent("alpha", model="tools")
    real_access = wt.os.access
    monkeypatch.setattr(
        wt.os, "access", lambda p, mode: not str(p).endswith("ro.py") and real_access(p, mode)
    )
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "rw.py", "content": "A = 1\n"}),
            ("write_file", {"path": "ro.py", "content": "B = 1\n"}),
            (
                "edit_file",
                {
                    "edits": [
                        {"path": "rw.py", "old_text": "A = 1", "new_text": "A = 2"},
                        {"path": "ro.py", "old_text": "B = 1", "new_text": "B = 2"},
                    ]
                },
            ),
            ("read_file", {"path": "rw.py"}),
        ],
    )
    assert "edit 2: file is not writable" in outputs[2] and "nothing was written" in outputs[2]
    assert "chmod -R u+rwX" in outputs[2]
    assert "     1\tA = 1" in outputs[3]


async def test_write_into_a_read_only_copy_is_returned_to_the_model_with_the_reason(
    container, make_agent, models
):
    # skill 只读副本 cp -R 出来，目录和文件都是 r-x/r--：写入失败交还给模型并指出要 chmod 的路径，不打崩运行（#483）
    me = await make_agent("alpha", model="tools")
    ro = container.agents.workspace(me.id) / "att"
    ro.mkdir(parents=True)
    (ro / "old.sh").write_text("echo 1\n")
    (ro / "old.sh").chmod(0o444)
    ro.chmod(0o555)
    shut = container.agents.workspace(me.id) / "shut"
    shut.mkdir()
    shut.chmod(0o000)  # 进不去的目录：连 stat 里面的文件都 EACCES，错误处理本身不能再抛
    try:
        outputs = await run_tools(
            container,
            models,
            me.id,
            [
                ("write_file", {"path": "att/new.sh", "content": "echo 2\n"}),
                ("write_file", {"path": "att/sub/x.sh", "content": "echo 3\n"}),
                ("write_file", {"path": "att/old.sh", "content": "echo 4\n"}),
                ("write_file", {"path": "ok.txt", "content": "fine\n"}),
                ("write_file", {"path": "shut/x.sh", "content": "echo 5\n"}),
            ],
        )
    finally:
        shut.chmod(0o755)
        ro.chmod(0o755)
        (ro / "old.sh").chmod(0o644)
    assert f"Permission denied: '{ro / 'new.sh'}'" in outputs[0]
    assert f"chmod -R u+rwX {ro}`" in outputs[0]
    assert f"chmod -R u+rwX {ro}`" in outputs[1]
    assert f"chmod -R u+rwX {ro / 'old.sh'}`" in outputs[2]
    assert outputs[3].startswith("created ")
    assert f"chmod -R u+rwX {shut}`" in outputs[4]
    assert (ro / "old.sh").read_text() == "echo 1\n"


async def test_edit_file_locates_by_line_range_or_pattern_and_validates_the_locator(
    container, make_agent, models
):
    me = await make_agent("alpha", model="tools")
    code = "import os\n\n\ndef keep():\n    return 1\n\n\ndef drop():\n    return 2\n"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "m.py", "content": code}),
            ("edit_file", {"path": "m.py", "pattern": "def drop", "new_text": ""}),
            (
                "edit_file",
                {
                    "path": "m.py",
                    "start_line": 4,
                    "end_line": 5,
                    "anchor": "def keep():",
                    "new_text": "K = 1",
                },
            ),
            (
                "edit_file",
                {"path": "m.py", "start_line": 4, "end_line": 4, "anchor": "def keep():", "new_text": ""},
            ),
            (
                "edit_file",
                {
                    "path": "m.py",
                    "edits": [
                        {"start_line": 4, "end_line": 4, "anchor": "K = 1", "new_text": "K = 2"},
                        {"pattern": "import $M", "new_text": "import $M, sys"},
                    ],
                },
            ),
            ("edit_file", {"path": "m.py", "edits": [{"old_text": "K", "pattern": "K", "new_text": "x"}]}),
            ("edit_file", {"path": "m.py", "start_line": 1, "new_text": "x"}),
            (
                "edit_file",
                {
                    "path": "m.py",
                    "start_line": 1,
                    "end_line": 1,
                    "anchor": "import os, sys",
                    "new_text": "x",
                    "replace_all": True,
                },
            ),
            (
                "edit_file",
                {
                    "path": "m.py",
                    "edits": [
                        {"old_text": "K = 2", "new_text": "K = 3"},
                        {"pattern": "def nothing", "new_text": ""},
                    ],
                },
            ),
            ("write_file", {"path": "conf.toml", "content": "a = 1\n"}),
            ("edit_file", {"path": "conf.toml", "pattern": "a = 1", "new_text": "a = 2"}),
            ("read_file", {"path": "m.py"}),
        ],
    )
    # 删掉函数连同它前面多出来的空行
    assert "(1 replacement; now 5 lines)" in outputs[1]
    assert "drop" not in outputs[1]
    assert "(1 replacement; now 4 lines)" in outputs[2] and "     4\tK = 1" in outputs[2]
    assert "anchor does not match line 4" in outputs[3] and "     4\tK = 1" in outputs[3]
    assert "(2 replacements; now 4 lines)" in outputs[4]
    assert "exactly one way" in outputs[5]
    assert "go together" in outputs[6]
    assert "replace_all does not apply to a line range" in outputs[7]
    assert (
        "edit 2 of 2" in outputs[8]
        and "matches nothing" in outputs[8]
        and "nothing was written" in outputs[8]
    )
    assert "extension '.toml'" in outputs[10]
    assert outputs[11] == "     1\timport os, sys\n     2\t\n     3\t\n     4\tK = 2"


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
                        {"path": "a.txt", "offset": 2, "limit": 2},
                        {"path": "missing.txt"},
                        {"path": "a.txt", "offset": 9},
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
    assert (
        "a.txt lines 2-3 of 10 <==\n     2\tline2\n     3\tline3\n[7 more lines; continue with offset=4]"
        in out
    )
    assert "missing.txt: file not found" in out
    assert "a.txt lines 9-10 of 10 <==\n     9\tline9\n    10\tline10" in out
    assert "b.txt lines 1-1 of 1 <==\n     1\tonly" in out
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
    # 与 read_file 同为 cat -n 格式，不连续的块之间是 `--`；插入处前后各 2 行上下文
    assert edited.startswith("edited ") and edited.endswith(
        "(2 replacements; now 20 lines); cat -n of the changed lines with 2 lines of context:\n"
        "     2\tl2\n     3\tl3\n     4\tnew\n     5\tl4\n     6\tl5\n"
        # 删除处只剩上下文，行号是改后的
        "--\n    14\tl13\n    15\tl14\n    16\tl16\n    17\tl17"
    )
    assert "the text is unchanged" in outputs[3]
    assert "only line endings changed" in outputs[5]
    assert "now 0 lines); the file is now empty" in outputs[6]


@pytest.fixture
async def small_output_container(settings, models):
    from muliao.container import assemble
    from muliao.platform.db import migrate

    settings = settings.model_copy(
        update={"runtime": settings.runtime.model_copy(update={"tool_output_max_chars": 1000})}
    )
    c = assemble(settings, resolve_model=models, model_checker=lambda _m, _t: None)
    migrate(settings.db_path)
    yield c
    await c.db.engine.dispose()


# 多到两段短正文加未读列表放不进 1000 字符、一段加列表放得进
N_SKIPPED = 30


async def test_read_file_stops_at_a_line_boundary_at_the_output_limit_and_says_where_to_continue(
    small_output_container, models
):
    from muliao.agents.service import AgentSpec

    container = small_output_container
    me = await container.agents.create(AgentSpec(name="alpha", description="d", model="tools"), actor="user")
    content = "".join(f"line{i:03}\n" for i in range(1, 301))
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": "a.txt", "content": content}),
            ("read_file", {"path": "a.txt"}),
            ("read_file", {"path": "a.txt", "offset": 11, "limit": 200}),
            ("read_file", {"reads": [{"path": "a.txt", "limit": 5}, {"path": "a.txt"}, {"path": "b.txt"}]}),
            ("write_file", {"path": "wide.txt", "content": "w" * 1500 + "\n"}),
            # wide.txt 第一行就超预算、未读的段很多：都挤进未读列表，列表不被截
            (
                "read_file",
                {
                    "reads": [
                        {"path": "a.txt", "limit": 3},
                        {"path": "a.txt", "offset": 11, "limit": 3},
                        {"path": "wide.txt"},
                        *({"path": f"d{i:02}.py", "offset": i} for i in range(1, N_SKIPPED + 1)),
                    ]
                },
            ),
        ],
    )
    single = outputs[1]
    assert len(single) <= 1000 and "truncated" not in single
    body, note = single.rsplit("\n", 1)
    last = int(body.rsplit("\n", 1)[1].split("\t")[0])
    # 最后一行完整，续读说明指向下一行（offset 与输出的行号同为 1 起算）
    assert body.endswith(f"{last:6}\tline{last:03}")
    assert note == f"[output limit reached at line {last} of 300; continue with offset={last + 1}]"
    ranged = outputs[2]
    stop = int(ranged.rsplit("limit reached at line ", 1)[1].split(" ")[0])
    assert ranged.startswith("    11\tline011") and ranged.endswith(
        f"continue with offset={stop + 1}, limit={210 - stop}]"
    )
    multi = outputs[3]
    assert len(multi) <= 1000 and "truncated" not in multi
    assert "a.txt lines 1-5 of 300 <==" in multi and "\n[295 more lines; continue with offset=6]" in multi
    second = multi.split("\n\n")[1]
    stop = int(second.rsplit("limit reached at line ", 1)[1].split(" ")[0])
    assert f"a.txt lines 1-{stop} of 300 <==" in second and second.endswith(
        f"continue with offset={stop + 1}]"
    )
    assert multi.endswith("\n\n[not read, output limit reached; read in another call: b.txt offset=1]")
    many = outputs[5]
    assert len(many) <= 1000 and "truncated" not in many
    # 列表放不下时连已读的第二段也挤进列表，只剩第一段；其余按次序列出
    head, tail = many.split("\n\n[not read, output limit reached; read in another call: ")
    assert head.startswith("==> ") and head.endswith(
        "a.txt lines 1-3 of 300 <==\n     1\tline001\n     2\tline002\n     3\tline003\n"
        "[297 more lines; continue with offset=4]"
    )
    listed = tail.removesuffix("]").split("; ")
    assert listed[:2] == ["a.txt offset=11 limit=3", "wide.txt offset=1"]
    assert listed[2:] == [f"d{i:02}.py offset={i}" for i in range(1, N_SKIPPED + 1)]


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
    assert (
        "…[line truncated: 3000 more chars]" in outputs[1]
        and "     2\tok" in outputs[1]
        and len(outputs[1]) < 2100
    )


async def test_read_file_returns_an_image_file_as_an_image_the_model_sees_and_stores_only_a_reference(
    container, make_agent, models
):
    import io

    from PIL import Image
    from pydantic_ai.messages import BinaryContent, ImageUrl

    from muliao.files.api import parse_file_url
    from tests.fixtures.echo_mcp import PNG as png
    from tests.test_flow import history_of

    me = await make_agent("alpha", model="tools")
    workspace = container.agents.workspace(me.id)
    (workspace / "shot.png").write_bytes(png)
    Image.new("RGB", (300, 9000), "white").save(workspace / "long.png")
    (workspace / "broken.jpg").write_bytes(b"not a jpeg")
    # 存不进文件存储的图片在 read_file 就拒绝（否则 ToolImagesToFiles 存不下、整次运行失败）
    container.files.max_bytes = (workspace / "long.png").stat().st_size
    (workspace / "huge.png").write_bytes(b"x" * (container.files.max_bytes + 1))
    seen: list[object] = []
    calls = [
        ("read_file", {"path": "shot.png"}),
        ("read_file", {"path": "shot.png", "offset": 3}),
        ("read_file", {"reads": [{"path": "shot.png"}]}),
        ("read_file", {"path": "long.png"}),
        ("read_file", {"path": "broken.jpg"}),
        ("read_file", {"path": "gone.webp"}),
        ("read_file", {"path": "huge.png"}),
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
    assert caption.endswith(f"shot.png (image/png, {len(png)} bytes)")
    assert image == BinaryContent(png, media_type="image/png")
    assert "offset/limit do not apply to an image file" in str(seen[1])
    assert "an image file cannot be read in reads" in str(seen[2])
    # 超过 provider 上限的长图切成几段交给模型，前面写明怎么切的（#336：原样发出去整个会话之后都是 400）
    caption, note, *tiles = seen[3]
    assert "long.png (image/png, " in caption
    assert (
        note
        == "[image 300x9000 split into 6 parts shown top to bottom, each scaled to fit 1568x1568, to fit the model's limits]"
    )
    assert [Image.open(io.BytesIO(t.data)).size for t in tiles] == [(300, 1500)] * 6
    # 解不成图片的不发给 provider，换成一句说明
    assert seen[4][1] == "[image 'read_file.jpg' could not be decoded as an image, so it is not shown]"
    assert "file not found" in str(seen[5])
    assert "image too large to view" in str(seen[6]) and "make a smaller copy first" in str(seen[6])
    # 历史里只存文件引用（ToolImagesToFiles），内容在文件存储里
    [stored, *_] = [
        p for m in await history_of(container, session.id) for p in m.parts if p.part_kind == "tool-return"
    ]
    [_, ref] = stored.content
    assert isinstance(ref, ImageUrl)
    [meta] = await container.files.referenced([parse_file_url(ref.url)[0]])
    assert (meta.name, container.files.read(meta)) == ("read_file.png", png)


async def test_the_skill_index_lists_own_skills_and_those_given_by_roles(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    await container.skills.write("good", "a good skill", "x", actor="user", owner=None)
    await container.skills.write("hidden", "not given", "x", actor="user", owner=None)
    await container.skills.write("mine", "my skill", "x", actor=alpha, owner=alpha)
    await share(container, "use:skill:good")
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info.instructions or "")
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    index = seen[0][seen[0].index("## Skill index") :]
    assert "- `good`: a good skill\n- `mine` (yours): my skill\n\n" in index and "hidden" not in index


THINKS = ModelProfile(supports_thinking=True)


def _thinking_seen(seen: list[object]) -> Callable[[list[ModelMessage], AgentInfo], ModelResponse]:
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # pydantic_ai 把 ModelSettings.thinking 移到请求参数上再交给 provider 换算
        seen.append(info.model_request_parameters.thinking)
        return ModelResponse(parts=[TextPart("ok")])

    return fn


async def test_agent_updates_its_thinking_level_and_bad_levels_go_back_to_the_model(
    container, make_agent, models
):
    me = await make_agent("manager", model="tools", roles=["admin"])
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("update_agent", {"agent": "manager", "thinking": "low"}),
            ("get_agent", {"agent": "manager"}),
            ("update_agent", {"agent": "manager", "thinking": "max"}),
        ],
        THINKS,
    )

    assert json.loads(outputs[1])["thinking"] == "low"
    assert "'xhigh'" in outputs[2]  # 不在档位里：交还模型重试
    assert (await container.agents.get(me.id)).thinking == "low"


async def test_agent_run_sends_its_thinking_level(container, make_agent, models):
    seen: list[object] = []
    models.models["m"] = scripted(_thinking_seen(seen), THINKS)
    default = await make_agent("plain", model="m")
    thinker = await make_agent("thinker", model="m")
    await container.agents.update(thinker.id, AgentPatch(thinking="xhigh"), actor="user")
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
        await container.agents.update(me.id, AgentPatch(thinking="off"), actor="user")
    await container.agents.update(me.id, AgentPatch(thinking="medium"), actor="user")
    with pytest.raises(InvalidInput, match="does not take a thinking setting"):
        await container.agents.update(me.id, AgentPatch(model="no-thinking"), actor="user")
    assert (await container.agents.get(me.id)).model == "always"
