"""工具权限（RBAC）：权限 id 与通配符规则、内置角色 admin / agent、自建角色、按规则算出的工具列表。MCP 不归权限管，
agent 用自己的私有 server 与勾选的公共 server（#180）。"""

import sys

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import McpTransport, RoleSpec, Rules, mcp_config
from muliao.agents.permissions import (
    ADMIN,
    AGENT,
    BUILTIN_ROLES,
    GLOBAL_WRITES,
    TOOL_PERMISSIONS,
    check_patterns,
)
from muliao.agents.service import AgentPatch, AgentSpec
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.sessions.api import Origin
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start
from tests.test_triggers import ECHO_SERVER

# 写所有 agent 共享的资源与管理 agent 的工具：agent 角色没有
_GLOBAL_WRITE_TOOLS = {
    "write_public_skill",
    "edit_public_skill",
    "delete_public_skill",
    "memory_edit_public",
    "vault_set_public",
    "vault_delete_public",
    "mcp_add_public",
    "mcp_remove_public",
    "mcp_set_enabled_public",
    "create_agent",
    "update_agent",
    "delete_agent",
}


def test_deny_wins_over_every_allow():
    rules = Rules(allow=("*",), deny=("agent:delete", "skill.public:*"))
    assert rules.allows("agent:create")
    assert not rules.allows("agent:delete")
    assert not rules.allows("skill.public:write")
    # memory: 与 memory.public: 是不同的资源，通配符不会串
    assert Rules(allow=("memory:*",)).allows("memory:edit")
    assert not Rules(allow=("memory:*",)).allows("memory.public:edit")


def test_union_of_roles_and_own_rules_keeps_every_deny():
    union = Rules.union([Rules(allow=("issue:*",)), Rules(allow=("*",), deny=("issue:update",))])
    assert union.allows("issue:create") and not union.allows("issue:update")


def test_patterns_must_match_a_known_permission():
    check_patterns(["*", "issue:*", "skill.public:write", "memory.public:*"])
    # MCP 不归工具权限管
    for bad in ("isue:list", "mcp:echo:*"):
        with pytest.raises(InvalidInput, match="match no tool permission"):
            check_patterns([bad])
    with pytest.raises(InvalidInput, match="distinct"):
        check_patterns(["issue:*", "issue:*"])


def test_every_tool_has_a_unique_permission_id():
    ids = [p.id for p in TOOL_PERMISSIONS.values()]
    assert len(ids) == len(set(ids))


def test_builtin_roles_are_admin_with_everything_and_agent_without_global_writes():
    every = list(TOOL_PERMISSIONS)
    assert list(BUILTIN_ROLES) == [ADMIN, AGENT]
    assert BUILTIN_ROLES[ADMIN].rules.tools(every) == every
    agent = BUILTIN_ROLES[AGENT].rules.tools(every)
    assert set(every) - set(agent) == _GLOBAL_WRITE_TOOLS
    # 每条全局写的规则都真的去掉了工具（拼错的会悄悄什么都不去掉）
    for pattern in GLOBAL_WRITES:
        check_patterns([pattern])


def test_the_agent_role_does_not_block_other_grants_of_global_writes():
    """agent 角色是纯 allow：自己的 allow 或别的角色（包括 admin）能放开全局写，不被它压住。"""
    agent = BUILTIN_ROLES[AGENT].rules
    assert not agent.deny
    assert Rules.union([agent, Rules(allow=("skill.public:write",))]).allows("skill.public:write")
    every = list(TOOL_PERMISSIONS)
    assert Rules.union([agent, BUILTIN_ROLES[ADMIN].rules]).tools(every) == every


async def test_new_agents_default_to_the_agent_role(container):
    agent = await container.agents.create(AgentSpec(name="a", description="d"), author="user")
    assert agent.roles == ("agent",)
    tools = container.agents.resolve(await container.agents.rules(agent)).tools
    assert not set(tools) & _GLOBAL_WRITE_TOOLS and {"run_shell", "memory_edit", "vault_set"} <= set(tools)


async def test_roles_are_created_assigned_updated_and_protected(container):
    roles = container.roles
    await roles.create(RoleSpec("reviewer", "reads issues and comments", ["issue:*"], ["issue:update"]))
    with pytest.raises(Conflict):
        await roles.create(RoleSpec("reviewer", "again", ["issue:*"], []))
    with pytest.raises(Conflict):
        await roles.create(RoleSpec("admin", "shadow a built-in", ["*"], []))
    with pytest.raises(InvalidInput, match="match no tool permission"):
        await roles.create(RoleSpec("typo", "d", ["isue:*"], []))
    with pytest.raises(InvalidInput, match="built in"):
        await roles.update(RoleSpec("agent", "d", ["issue:*"], []))
    with pytest.raises(NotFound):
        await roles.delete("nope")

    agent = await container.agents.create(
        AgentSpec(name="r", description="d", roles=["reviewer"]), author="user"
    )
    tools = container.agents.resolve(await container.agents.rules(agent)).tools
    assert tools == ["issue_list", "issue_show", "issue_create", "issue_comment"]

    # 角色改动从下一次计算起生效
    await roles.update(RoleSpec("reviewer", "reads issues", ["issue:list"], []))
    assert container.agents.resolve(await container.agents.rules(agent)).tools == ["issue_list"]

    with pytest.raises(Conflict, match="used by agents"):
        await roles.delete("reviewer")
    await container.agents.update(agent.id, AgentPatch(roles=["agent"]))
    await roles.delete("reviewer")
    assert "reviewer" not in [r.name for r in await roles.list()]
    with pytest.raises(InvalidInput, match="roles must be"):
        await container.agents.create(AgentSpec(name="x", description="d", roles=["public"]), author="user")


async def test_an_agent_can_deny_itself_tools_on_top_of_its_role(container):
    agent = await container.agents.create(
        AgentSpec(name="quiet", description="d", deny=["trigger:*", "shell:run"]), author="user"
    )
    tools = container.agents.resolve(await container.agents.rules(agent)).tools
    assert (
        not any(t.startswith("trigger_") for t in tools) and "run_shell" not in tools and "read_file" in tools
    )


async def test_mcp_is_its_private_servers_plus_the_public_ones_it_selected(container, models):
    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add(None, "echo", spec, probe=False, author="user")
    await container.mcp.add(None, "other", spec, probe=False, author="user")
    picky = await container.agents.create(
        AgentSpec(name="picky", description="d", model="m1", mcp_servers=["echo"]), author="user"
    )
    await container.mcp.add(picky.id, "mine", spec, probe=False, author="user")
    plain = await container.agents.create(AgentSpec(name="plain", description="d", model="m2"), author="user")
    seen: dict[str, set[str]] = {"m1": set(), "m2": set()}

    def recorder(model: str):
        def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            seen[model].update(t.name.split("_")[0] for t in info.function_tools if "_" in t.name)
            return ModelResponse(parts=[TextPart("ok")])

        return scripted(fn)

    models.models["m1"], models.models["m2"] = recorder("m1"), recorder("m2")
    await start(container, picky.id, "event", origin=Origin.TRIGGER)
    await start(container, plain.id, "event", origin=Origin.TRIGGER)
    await drain(container, limit_seconds=30)

    assert seen["m1"] & {"echo", "other", "mine"} == {"echo", "mine"}
    assert seen["m2"] & {"echo", "other", "mine"} == set()


async def test_only_existing_public_servers_can_be_newly_selected(container):
    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add(None, "echo", spec, probe=False, author="user")
    with pytest.raises(InvalidInput, match="not public MCP servers"):
        await container.agents.create(
            AgentSpec(name="a", description="d", mcp_servers=["nope"]), author="user"
        )
    agent = await container.agents.create(
        AgentSpec(name="a", description="d", mcp_servers=["echo"]), author="user"
    )
    # 勾选的 server 被删后留着的名字不拦别的修改
    await container.mcp.remove(None, "echo", "user")
    agent = await container.agents.update(agent.id, AgentPatch(description="new"))
    assert agent.mcp_servers == ("echo",)
    with pytest.raises(InvalidInput, match="distinct"):
        await container.agents.update(agent.id, AgentPatch(mcp_servers=["echo", "echo"]))


async def test_permissions_and_roles_api(client, container):
    await login(client)
    perms = (await client.get("/api/permissions")).json()
    assert {"tool": "delete_agent", "id": "agent:delete", "access": "write"} in perms

    resolved = (
        await client.post(
            "/api/permissions/resolve", json={"roles": ["agent"], "allow": [], "deny": ["memory:*"]}
        )
    ).json()
    names = [t["tool"] for t in resolved["tools"]]
    assert "run_shell" in names and "memory_edit" not in names and "write_public_skill" not in names
    bad = await client.post("/api/permissions/resolve", json={"roles": ["nope"], "allow": [], "deny": []})
    assert bad.status_code == 422

    body = {"description": "issues only", "allow": ["issue:*"], "deny": []}
    assert (await client.post("/api/roles/triage", json=body)).status_code == 201
    listed = {r["name"]: r for r in (await client.get("/api/roles")).json()}
    assert listed["agent"]["builtin"] and not listed["triage"]["builtin"]
    assert [t["tool"] for t in listed["triage"]["resolved"]["tools"]][:2] == ["issue_list", "issue_show"]
    assert (await client.put("/api/roles/admin", json=body)).status_code == 422

    agent = (
        await client.post("/api/agents", json={"name": "t", "description": "d", "roles": ["triage"]})
    ).json()
    assert (agent["roles"], agent["allow"], agent["deny"], agent["mcp_servers"]) == (["triage"], [], [], [])
    assert (await client.delete("/api/roles/triage")).status_code == 409
    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    # 删除 agent 的清理做完、行删掉之后角色才没人引用
    await drain(container)
    assert (await client.delete("/api/roles/triage")).status_code == 204
