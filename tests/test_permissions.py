"""工具权限（RBAC，#172）：权限 id 与通配符规则、角色、按规则算出的工具列表（含 MCP）。"""

import sys

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import McpTransport, RoleSpec, Rules, mcp_config
from muliao.agents.permissions import ADMIN, BUILTIN_ROLES, PUBLIC, TOOL_PERMISSIONS, check_patterns
from muliao.agents.service import AgentPatch, AgentSpec
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.sessions.api import Origin
from tests.conftest import login
from tests.scripted import scripted
from tests.test_flow import drain, start
from tests.test_triggers import ECHO_SERVER


def test_deny_wins_over_every_allow():
    rules = Rules(allow=("*",), deny=("agent:delete", "skill.public:*"))
    assert rules.allows("agent:create")
    assert not rules.allows("agent:delete")
    assert not rules.allows("skill.public:write")
    # mcp: 与 mcp.config: 是不同的资源，通配符不会串
    assert Rules(allow=("mcp:*",)).allows("mcp:echo:token")
    assert not Rules(allow=("mcp:*",)).allows("mcp.config:add")


def test_union_of_roles_and_own_rules_keeps_every_deny():
    union = Rules.union([Rules(allow=("issue:*",)), Rules(allow=("*",), deny=("issue:update",))])
    assert union.allows("issue:create") and not union.allows("issue:update")


@pytest.mark.parametrize(
    ("allow", "deny", "prefix", "may"),
    [
        (("*",), (), "mcp:", True),
        (("mcp:*",), (), "mcp:", True),
        (("m*",), (), "mcp:", True),
        (("issue:*",), (), "mcp:", False),
        ((), (), "mcp:", False),
        # deny 整段覆盖时不连接
        (("*",), ("mcp:*",), "mcp:", False),
        (("*",), ("mcp:echo:*",), "mcp:echo:", False),
        (("*",), ("mcp:echo:token",), "mcp:echo:", True),
        # 只放行某个 server：别的 server 不连接
        (("mcp:echo:*",), (), "mcp:other:", False),
        (("mcp:echo:*",), (), "mcp:echo:", True),
    ],
)
def test_mcp_servers_are_connected_only_when_the_rules_may_allow_one_of_their_tools(allow, deny, prefix, may):
    assert Rules(allow=allow, deny=deny).may_allow_prefix(prefix) is may


def test_patterns_must_match_a_known_permission_or_be_mcp():
    check_patterns(["*", "issue:*", "skill.public:write", "mcp:anything:*", "mcp:*", "mcp:echo:token"])
    for bad in ("isue:list", "mcp:echo"):
        with pytest.raises(InvalidInput, match="match no tool permission"):
            check_patterns([bad])
    with pytest.raises(InvalidInput, match="distinct"):
        check_patterns(["issue:*", "issue:*"])


def test_every_tool_has_a_unique_permission_id():
    ids = [p.id for p in TOOL_PERMISSIONS.values()]
    assert len(ids) == len(set(ids))


def test_the_old_tool_groups_as_roles_cover_exactly_what_admin_covers():
    """迁移把「全部工具组」原样变成同名角色：它们合起来（加 public）必须正好是全部工具，与 admin 一致。"""
    groups = [r for name, r in BUILTIN_ROLES.items() if name not in (PUBLIC, ADMIN)]
    union = Rules.union([BUILTIN_ROLES[PUBLIC].rules, *(r.rules for r in groups)])
    every = list(TOOL_PERMISSIONS)
    assert union.tools(every) == BUILTIN_ROLES[ADMIN].rules.tools(every) == every
    # 每个工具都只来自一个组角色（public 之外）：原来的组互不重叠
    for tool, p in TOOL_PERMISSIONS.items():
        owners = [r.name for r in groups if r.rules.allows(p.id)]
        assert len(owners) <= 1, (tool, owners)


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
        await roles.update(RoleSpec("admin", "d", ["issue:*"], []))
    with pytest.raises(NotFound):
        await roles.delete("nope")

    agent = await container.agents.create(
        AgentSpec(name="r", description="d", roles=["reviewer", "notify"]), author="user"
    )
    tools = container.agents.resolve(await container.agents.rules(agent)).tools
    assert {"issue_create", "issue_comment", "notify_user"} <= set(tools)
    assert "issue_update" not in tools and "run_shell" not in tools

    # 角色改动从下一次计算起生效
    await roles.update(RoleSpec("reviewer", "reads issues", ["issue:list"], []))
    tools = container.agents.resolve(await container.agents.rules(agent)).tools
    assert "issue_create" not in tools and "issue_list" in tools

    with pytest.raises(Conflict, match="used by agents"):
        await roles.delete("reviewer")
    await container.agents.update(agent.id, AgentPatch(roles=["notify"]))
    await roles.delete("reviewer")
    assert "reviewer" not in [r.name for r in await roles.list()]


async def test_an_agent_can_deny_itself_public_tools(container):
    agent = await container.agents.create(
        AgentSpec(name="quiet", description="d", roles=["notify"], deny=["memory:*"]), author="user"
    )
    tools = container.agents.resolve(await container.agents.rules(agent)).tools
    assert "memory_append" not in tools and "read_skill" in tools


async def test_mcp_tools_are_filtered_by_their_permission_ids(container, models):
    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add(None, "echo", spec, probe=False, author="user")
    agent = await container.agents.create(
        AgentSpec(
            name="m",
            description="d",
            model="m",
            roles=["notify"],
            allow=["mcp:echo:*"],
            deny=["mcp:echo:cwd"],
        ),
        author="user",
    )
    seen: list[str] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, agent.id, "event", origin=Origin.TRIGGER)
    await drain(container, limit_seconds=30)

    mcp = {n for n in seen if n.startswith("echo_")}
    assert mcp == {"echo_echo", "echo_token", "echo_screenshot"}


async def test_permissions_and_roles_api(client, container):
    await login(client)
    perms = (await client.get("/api/permissions")).json()
    assert {"tool": "delete_agent", "id": "agent:delete", "access": "irreversible", "note": ""} in perms

    resolved = (
        await client.post(
            "/api/permissions/resolve", json={"roles": ["notify"], "allow": [], "deny": ["memory:*"]}
        )
    ).json()
    names = [t["tool"] for t in resolved["tools"]]
    assert "notify_user" in names and "memory_append" not in names and resolved["mcp"] is False
    bad = await client.post("/api/permissions/resolve", json={"roles": ["nope"], "allow": [], "deny": []})
    assert bad.status_code == 422

    body = {"description": "issues only", "allow": ["issue:*"], "deny": []}
    assert (await client.post("/api/roles/triage", json=body)).status_code == 201
    listed = {r["name"]: r for r in (await client.get("/api/roles")).json()}
    assert listed["public"]["builtin"] and not listed["triage"]["builtin"]
    assert [t["tool"] for t in listed["triage"]["resolved"]["tools"]][:2] == ["issue_list", "issue_show"]
    assert (await client.put("/api/roles/admin", json=body)).status_code == 422

    agent = (
        await client.post("/api/agents", json={"name": "t", "description": "d", "roles": ["triage"]})
    ).json()
    assert (agent["roles"], agent["allow"], agent["deny"]) == (["triage"], [], [])
    assert (await client.delete("/api/roles/triage")).status_code == 409
    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    # 删除 agent 的清理做完、行删掉之后角色才没人引用
    await drain(container)
    assert (await client.delete("/api/roles/triage")).status_code == 204


# 改为角色之前每个 agent 总是有的工具（container 的 always 工具集）
_OLD_ALWAYS = {
    "read_skill",
    "write_skill",
    "edit_skill",
    "delete_skill",
    "memory_append",
    "memory_replace",
    "history_append",
    "history_search",
    "ask_user",
}


def test_public_plus_each_old_group_role_gives_the_old_tools_plus_issue_reads():
    """迁移只把 tool_groups 改名为 roles：对每个旧组 g，public + g 等于原来的 always + g；public 唯一多出的是读 issue
    （用户定的）。"""
    every = list(TOOL_PERMISSIONS)
    public = set(BUILTIN_ROLES[PUBLIC].rules.tools(every))
    assert public - _OLD_ALWAYS == {"issue_list", "issue_show"} and _OLD_ALWAYS <= public
    for name, role in BUILTIN_ROLES.items():
        if name in (PUBLIC, ADMIN):
            continue
        group = set(role.rules.tools(every))
        combined = set(Rules.union([BUILTIN_ROLES[PUBLIC].rules, role.rules]).tools(every))
        assert combined == _OLD_ALWAYS | group | {"issue_list", "issue_show"}, name


async def test_servers_whose_tools_the_rules_cannot_allow_are_not_connected(container, models, monkeypatch):
    from muliao.agent_runtime.mcp import McpConnector

    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add(None, "echo", spec, probe=False, author="user")
    await container.mcp.add(None, "other", spec, probe=False, author="user")
    built: list[str] = []
    original = McpConnector.build_toolset

    async def recording(self, server, cwd):  # noqa: ANN001, ANN202
        built.append(server.name)
        return await original(self, server, cwd)

    monkeypatch.setattr(McpConnector, "build_toolset", recording)
    agent = await container.agents.create(
        AgentSpec(name="m", description="d", model="m", roles=["notify"], allow=["mcp:echo:*"]), author="user"
    )
    models.models["m"] = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("ok")]))
    await start(container, agent.id, "event", origin=Origin.TRIGGER)
    await drain(container, limit_seconds=30)

    assert built == ["echo"]
