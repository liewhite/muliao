"""分发与归属（#235、#365）：`action:kind:name` 规则、内置角色 base / admin、自建角色；能用 = 自己的 ∪ 规则放行的（skill 索引、MCP、
secret）；能改 = owner 或规则放行 write（admin 的 write:*）。"""

import sys

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agent_runtime.api import memory_section
from muliao.agents.api import McpTransport, RoleSpec, Rules, StoredKind, mcp_config
from muliao.agents.kinds import as_kind
from muliao.agents.permissions import (
    ADMIN,
    BASE,
    BUILTIN_ROLES,
    Action,
    Kind,
    ToolSpec,
    ToolTable,
    fixed_rules,
    kind_of,
    matches_no_tool,
)
from muliao.agents.service import AgentPatch, AgentSpec
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.platform.tooling import Access
from muliao.sessions.api import Origin
from tests.conftest import login, owned
from tests.scripted import scripted
from tests.test_flow import drain, start
from tests.test_triggers import ECHO_SERVER

# 管理类工具：base 没有，admin 有
_ADMIN_TOOLS = {
    "create_agent",
    "delete_agent",
}


def test_rules_match_action_kind_and_name_and_deny_wins():
    rules = Rules(
        allow=("use:tool:*", "use:skill:relay-*", "use:secret:RELAY_*", "*:memory:trading"),
        deny=("use:tool:delete_agent",),
    )
    assert rules.includes(Kind.TOOL, "create_agent") and not rules.includes(Kind.TOOL, "delete_agent")
    assert rules.includes(Kind.SKILL, "relay-host") and not rules.includes(Kind.SKILL, "other")
    assert rules.includes(Kind.SECRET, "RELAY_KEY") and not rules.includes(Kind.MCP, "RELAY_KEY")
    # 动作互相独立：use 不含 write，`*` 两者都有
    assert not rules.allows(Action.WRITE, Kind.SKILL, "relay-host")
    assert rules.allows(Action.USE, Kind.MEMORY, "trading") and rules.allows(
        Action.WRITE, Kind.MEMORY, "trading"
    )
    writer = Rules(allow=("write:skill:relay-*",))
    assert writer.allows(Action.WRITE, Kind.SKILL, "relay-host") and not writer.includes(
        Kind.SKILL, "relay-host"
    )
    # admin 的 write:* 覆盖每一种的写，但不等于能用
    admin = fixed_rules(ToolTable.of([]))[ADMIN]
    assert admin.allows(Action.WRITE, Kind.MEMORY, "global") and not admin.includes(Kind.MEMORY, "global")
    # 种类不串：skill 规则不放行同名 MCP
    assert not Rules(allow=("use:skill:*",)).includes(Kind.MCP, "x")
    union = Rules.union(
        [Rules(allow=("use:tool:issue_*",)), Rules(allow=("use:tool:*",), deny=("use:tool:issue_update",))]
    )
    assert union.includes(Kind.TOOL, "issue_create") and not union.includes(Kind.TOOL, "issue_update")
    # 不按名字指的资产（私有记忆）：按种类与名字的规则管不到，只有不限种类的；admin 就是放行不限种类的写
    everything = Rules(allow=("*:memory:*", "*:skill:*"))
    assert not everything.allows(Action.USE, Kind.MEMORY, None) and not everything.allows_every(Action.WRITE)
    assert admin.allows(Action.WRITE, Kind.MEMORY, None) and admin.allows_every(Action.WRITE)
    assert not admin.includes(Kind.MEMORY, None)
    assert not Rules(allow=("write:*",), deny=("write:*",)).allows_every(Action.WRITE)
    # 针对某种资产的 deny 只收回那些，不影响「不限种类」
    narrowed = Rules(allow=("write:*",), deny=("write:memory:global",))
    assert narrowed.allows_every(Action.WRITE) and not narrowed.allows(Action.WRITE, Kind.MEMORY, "global")


def test_patterns_need_an_action_and_a_kind_and_tool_patterns_a_tool():
    tools = ToolTable.of(
        [ToolSpec("run_shell", Access.WRITE, False), ToolSpec("issue_list", Access.READ, False)]
    )
    for good in ["use:tool:*", "use:tool:issue_*", "use:skill:anything", "use:mcp:x", "use:secret:K_*"] + [
        "write:memory:m",
        "*:memory:m",
        "write:*",
        "*:tool:run_shell",
    ]:
        kind_of(good)
    for bad in (
        "issue:list",
        "*",
        "tool:run_shell",
        "skill:x",
        "use:tool:",
        "use:skills:x",
        "read:skill:x",
        ":x",
    ):
        with pytest.raises(InvalidInput, match="action:kind:name"):
            kind_of(bad)
    with pytest.raises(InvalidInput, match="only be used"):
        kind_of("write:tool:run_shell")
    # 是否匹配当前注册的工具单独判断（读树记问题、写入只查新加的）
    assert matches_no_tool("use:tool:isue_*", tools) and not matches_no_tool("use:tool:issue_*", tools)
    assert not matches_no_tool("use:skill:isue_*", tools)


async def test_base_has_every_tool_but_admin_ones_and_admin_has_all(container):
    # 工具表由各工具注册时的声明收集而来（agent_runtime.Toolbox.specs）
    table = (await container.assets.snapshot()).tools
    every = list(table.names)
    fixed = fixed_rules(table)
    assert tuple(fixed) == BUILTIN_ROLES == (BASE, ADMIN)
    assert fixed[ADMIN].tools(every) == every
    assert set(every) - set(fixed[BASE].tools(every)) == _ADMIN_TOOLS
    assert {t.name for t in table.specs if t.admin} == _ADMIN_TOOLS
    # base 是纯 allow：admin 角色或自己的 allow 能放开管理工具，不被它压住
    assert not fixed[BASE].deny


async def test_new_agents_have_base_implicitly(container):
    agent = await container.agents.create(AgentSpec(name="a", description="d"), actor="user")
    assert (await container.agents.view(agent.id)).rules.roles == ()
    tools = (await container.distribution.reach(agent.id)).tool_names
    assert not set(tools) & _ADMIN_TOOLS and {"run_shell", "edit_file", "vault_set"} <= set(tools)
    with pytest.raises(InvalidInput, match="implicit"):
        await container.agents.create(AgentSpec(name="b", description="d", roles=[BASE]), actor="user")
    admin = await container.agents.create(AgentSpec(name="c", description="d", roles=[ADMIN]), actor="user")
    assert (await container.distribution.reach(admin.id)).tool_names == list(
        await container.distribution.tool_names()
    )


async def test_roles_are_created_assigned_updated_and_protected(container):
    roles = container.distribution
    await roles.create(RoleSpec("reviewer", "reads issues", ["use:tool:issue_*"], ["use:tool:issue_update"]))
    with pytest.raises(Conflict):
        await roles.create(RoleSpec("reviewer", "again", ["use:tool:issue_*"], []))
    with pytest.raises(Conflict):
        await roles.create(RoleSpec("admin", "shadow a built-in", ["use:tool:*"], []))
    with pytest.raises(InvalidInput, match="no existing tool"):
        await roles.create(RoleSpec("typo", "d", ["use:tool:isue_*"], []))
    with pytest.raises(InvalidInput, match="no existing"):
        await roles.create(RoleSpec("typo", "d", ["use:skill:nope"], []))
    with pytest.raises(InvalidInput, match="built in"):
        await roles.delete(BASE)
    with pytest.raises(NotFound):
        await roles.delete("nope")

    agent = await container.agents.create(
        AgentSpec(name="r", description="d", deny=["use:tool:*"], roles=["reviewer"]), actor="user"
    )
    # 自己的 deny 盖过 base 与角色
    assert (await container.distribution.reach(agent.id)).tool_names == []
    agent = await container.agents.update(agent.id, AgentPatch(deny=["use:tool:run_shell"]), actor="user")
    assert "issue_update" not in (await container.distribution.reach(agent.id)).tool_names

    with pytest.raises(Conflict, match="used by agents"):
        await roles.delete("reviewer")
    await container.agents.update(agent.id, AgentPatch(roles=[]), actor="user")
    await roles.delete("reviewer")
    assert [r.name for r in await roles.list()] == [BASE, ADMIN]
    with pytest.raises(InvalidInput, match=r"not defined in \[roles\]"):
        await container.agents.create(AgentSpec(name="x", description="d", roles=["public"]), actor="user")
    with pytest.raises(InvalidInput, match="implicit"):
        await container.agents.create(AgentSpec(name="x", description="d", roles=[BASE]), actor="user")
    with pytest.raises(InvalidInput, match="distinct"):
        await container.agents.create(
            AgentSpec(name="x", description="d", allow=["use:tool:*", "use:tool:*"]), actor="user"
        )


async def test_a_role_hands_out_skills_and_secrets_and_an_assets_owner_always_has_it(container, make_agent):
    owner, trader, other = await make_agent("owner"), await make_agent("trader"), await make_agent("other")
    await container.skills.write("relay-host", "d", "body", version=None, owner=owner.id)
    await container.vault.set("RELAY_KEY", "v", "", actor=owner.id, owner=owner.id)
    assert [s.name for s in await container.skills.index(owner.id)] == ["relay-host"]
    assert await container.skills.index(trader.id) == []

    await container.distribution.create(
        RoleSpec("relay-trader", "relay", ["use:skill:relay-host", "use:secret:RELAY_*"], [])
    )
    await container.agents.update(trader.id, AgentPatch(roles=["relay-trader"]), actor="user")
    assert [s.name for s in await container.skills.index(trader.id)] == ["relay-host"]
    assert await container.vault.resolve_many(trader.id, ["RELAY_KEY"], shell=False) == {"RELAY_KEY": "v"}
    # 记忆：base 收录的全局记忆与它自己的私有记忆
    assert (await container.distribution.reach(trader.id)).assets == [
        "memory:global",
        "memory:trader",
        "secret:RELAY_KEY",
        "skill:relay-host",
    ]
    with pytest.raises(NotFound):
        await container.vault.resolve_many(other.id, ["RELAY_KEY"], shell=False)

    # deny 只作用于规则分来的，不作用于自己的
    await container.agents.update(owner.id, AgentPatch(deny=["use:skill:*"]), actor="user")
    assert [s.name for s in await container.skills.index(owner.id)] == ["relay-host"]
    await container.agents.update(trader.id, AgentPatch(deny=["use:skill:*"]), actor="user")
    assert await container.skills.index(trader.id) == []


async def test_only_the_owner_an_admin_or_the_user_can_change_a_secret(container, make_agent):
    owner, stranger = await make_agent("owner"), await make_agent("stranger")
    admin = await make_agent("boss", roles=(ADMIN,))
    await container.vault.set("K", "v", "", actor=owner.id, owner=owner.id)
    with pytest.raises(Forbidden, match="belongs to agent 'owner'"):
        await container.vault.set("K", "x", "", actor=stranger.id, owner=stranger.id)
    # admin 改别人的：owner 不变
    await container.vault.set("K", "by admin", "", actor=admin.id, owner=admin.id)
    assert (await container.vault.get("K")).owner == owner.id
    await container.vault.set("P", "v", "", actor="user", owner=None)
    with pytest.raises(Forbidden, match="belongs to the platform"):
        await container.vault.set("P", "x", "", actor=owner.id, owner=owner.id)
    await container.vault.set("P", "x", "", actor=admin.id, owner=admin.id)


async def test_a_deleted_agents_shared_assets_move_to_the_platform(container, make_agent):
    owner, user = await make_agent("owner"), await make_agent("user1")
    await container.skills.write("shared", "d", "b", version=None, owner=owner.id)
    await container.skills.write("alone", "d", "b", version=None, owner=owner.id)
    await container.distribution.patch(BASE, allow=["use:skill:shared"], deny=[])
    await container.agent_lifecycle.delete(owner.id, actor="user")
    await drain(container)
    assert await owned(container, "skill", None) == ["shared"]
    assert [s.name for s in await container.skills.index(user.id)] == ["shared"]
    platform = container.settings.assets_dir / "platform" / "skills"
    assert (platform / "shared" / "SKILL.md").exists()
    assert not (platform / "alone").exists()


async def test_transferring_an_item_moves_who_owns_and_uses_it(container, make_agent):
    old, new = await make_agent("old"), await make_agent("new")
    await container.skills.write("s", "d", "b", version=None, owner=old.id)
    await container.vault.set("K", "x", "", actor=old.id, owner=old.id)
    await container.catalog.transfer(StoredKind.SKILL, "s", new.id)
    await container.catalog.transfer(StoredKind.SECRET, "K", None)
    assert await owned(container, "skill", new.id) == ["s"]
    assert await owned(container, "secret", None) == ["K"]
    assert [s.name for s in await container.skills.index(new.id)] == ["s"]
    assert await container.skills.index(old.id) == []


async def test_transfer_rejects_no_ops_and_unknown_owners(container, make_agent):
    a = await make_agent("a")
    await container.skills.write("s", "d", "b", version=None, owner=a.id)
    with pytest.raises(InvalidInput, match="already belongs"):
        await container.catalog.transfer(StoredKind.SKILL, "s", a.id)
    with pytest.raises(NotFound):
        await container.catalog.transfer(StoredKind.SKILL, "s", "nobody")
    with pytest.raises(NotFound, match="MCP server 's' not found"):
        await container.catalog.transfer(StoredKind.MCP, "s", None)
    assert await owned(container, "skill", a.id) == ["s"]


async def test_transfer_asset_api(client, container, make_agent):
    await login(client)
    a = await make_agent("a")
    await container.skills.write("s", "d", "b", version=None, owner=None)
    resp = await client.put("/api/assets/skill/s/owner", json={"owner": a.id})
    assert resp.status_code == 204
    assert await owned(container, "skill", a.id) == ["s"]
    assert (await client.put("/api/assets/tool/run_shell/owner", json={"owner": None})).status_code == 422


async def test_mcp_is_what_the_agent_owns_or_its_roles_give_it(container, models):
    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add("echo", spec, owner=None, probe=False)
    await container.mcp.add("other", spec, owner=None, probe=False)
    await container.distribution.create(RoleSpec("echoer", "echo", ["use:mcp:echo"], []))
    picky = await container.agents.create(
        AgentSpec(name="picky", description="d", model="m1", roles=["echoer"]), actor="user"
    )
    await container.mcp.add("mine", spec, owner=picky.id, probe=False)
    plain = await container.agents.create(AgentSpec(name="plain", description="d", model="m2"), actor="user")
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
    # 停用的不连接
    await container.mcp.set_enabled("echo", False)
    assert [s.name for s in await container.mcp.index(picky.id)] == ["mine"]


async def test_an_item_rule_survives_its_item_being_removed(container):
    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add("echo", spec, owner=None, probe=False)
    await container.distribution.create(RoleSpec("echoer", "echo", ["use:mcp:echo"], []))
    await container.catalog.delete(StoredKind.MCP, "echo")
    # 已有的规则不查是否匹配现有资产：改描述照常
    await container.distribution.patch("echoer", description="new", allow=["use:mcp:echo"])
    with pytest.raises(InvalidInput, match="no existing"):
        await container.distribution.patch("echoer", allow=["use:mcp:echo", "use:mcp:gone"])


async def test_tools_and_roles_api(client, container):
    await login(client)
    items = {i["name"]: i for i in (await client.get("/api/assets")).json() if i["kind"] == "tool"}
    assert (items["delete_agent"]["access"], items["delete_agent"]["admin"]) == ("write", True)
    assert (items["run_shell"]["access"], items["run_shell"]["admin"]) == ("write", False)
    assert (await client.get("/api/tools")).status_code == 404

    def preview(agent_id=None, roles=(), allow=(), deny=()):
        body = {"agent_id": agent_id, "roles": list(roles), "allow": list(allow), "deny": list(deny)}
        return client.post("/api/agent-preview", json=body)

    resolved = (await preview(deny=["use:tool:edit_*"])).json()
    names = [t["name"] for t in resolved["tools"]]
    assert "run_shell" in names and "edit_file" not in names and "create_agent" not in names
    assert (await preview(roles=["nope"])).status_code == 422
    assert (await preview(allow=["use:skill:nope"])).status_code == 422

    await container.skills.write("s", "d", "b", version=None, owner=None)
    body = {"description": "issues only", "allow": ["use:tool:issue_*", "use:skill:s"], "deny": []}
    role_preview = (
        await client.post("/api/role-preview", json={"name": None, "allow": body["allow"], "deny": []})
    ).json()
    assert role_preview["assets"] == ["skill:s"]
    assert [t["name"] for t in role_preview["tools"]][:2] == ["issue_list", "issue_show"]
    bad_role = {"name": None, "allow": ["use:skill:nope"], "deny": []}
    assert (await client.post("/api/role-preview", json=bad_role)).status_code == 422
    assert (await client.post("/api/roles/triage", json=body)).status_code == 201
    listed = {r["name"]: r for r in (await client.get("/api/roles")).json()}
    assert listed[BASE]["builtin"] and not listed["triage"]["builtin"]
    # base 每个 agent 隐含：界面上不能选（与写入时的校验同一个定义，Role.assignable）
    assert not listed[BASE]["assignable"] and listed["admin"]["assignable"] and listed["triage"]["assignable"]
    assert "use:tool:run_shell" in listed[BASE]["fixed_allow"]
    assert [t["name"] for t in listed["triage"]["reach"]["tools"]][:2] == ["issue_list", "issue_show"]
    assert listed["triage"]["reach"]["assets"] == ["skill:s"] and listed[BASE]["reach"]["assets"] == [
        "memory:global"
    ]
    # 内置角色的预览含它固定的部分
    base_preview = (
        await client.post("/api/role-preview", json={"name": BASE, "allow": [], "deny": []})
    ).json()
    assert "run_shell" in [t["name"] for t in base_preview["tools"]]
    # 内置角色可编辑的部分可以改，不能删
    assert (await client.patch("/api/roles/admin", json=body)).status_code == 200
    assert (await client.delete("/api/roles/admin")).status_code == 422

    agent = (
        await client.post("/api/agents", json={"name": "t", "description": "d", "roles": ["triage"]})
    ).json()
    assert (agent["roles"], agent["allow"], agent["deny"]) == (["triage"], [], [])
    listed = {r["name"]: r for r in (await client.get("/api/roles")).json()}
    assert listed["triage"]["agents"] == [agent["id"]] and agent["id"] in listed[BASE]["agents"]
    assert listed["admin"]["agents"] == []
    # 自己的资产总能用，deny 只拦规则分来的
    await container.skills.write("own", "d", "b", version=None, owner=agent["id"])
    reach = (await preview(agent["id"], ["triage"], deny=["use:skill:*"])).json()
    assert reach["assets"] == ["memory:global", "memory:t", "skill:own"]
    assert (await preview(None, ["triage"])).json()["assets"] == ["memory:global", "skill:s"]
    assert (await client.delete("/api/roles/triage")).status_code == 409
    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    # 删除 agent 的清理做完、行删掉之后角色才没人引用
    await drain(container)
    assert (await client.delete("/api/roles/triage")).status_code == 204


async def test_agent_assets_group_by_kind_with_sources_and_memory_preview(client, container):
    await login(client)
    await container.skills.write("s", "shared skill", "b", version=None, owner=None)
    await container.skills.write("hidden", "d", "b", version=None, owner=None)
    await container.vault.set("K", "secret-value", "a key", actor="user", owner=None)
    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add("echo", spec, owner=None, probe=False)
    await container.memory.put("team", "- team fact", version=None)
    await container.distribution.create(
        RoleSpec("triage", "t", ["use:skill:s", "*:memory:team", "use:skill:hidden"], [])
    )
    agent = await container.agents.create(
        AgentSpec(
            name="t",
            description="d",
            model="m1",
            roles=["triage"],
            allow=["use:secret:K", "use:mcp:echo", "use:skill:s"],
            deny=["use:skill:hidden"],
        ),
        actor="user",
    )
    await container.skills.write("own", "mine", "b", version=None, owner=agent.id)
    # deny 不拦自己名下的
    await container.agents.update(
        agent.id, AgentPatch(deny=["use:skill:hidden", "use:skill:own"]), actor="user"
    )

    got = (await client.get(f"/api/agents/{agent.id}/assets")).json()
    src = {g: {a["name"]: a["source"] for a in got[g]} for g in ("memories", "skills", "secrets", "mcp")}
    assert src["skills"] == {
        "own": {"own": True, "roles": [], "allow": False},
        "s": {"own": False, "roles": ["triage"], "allow": True},
    }
    assert src["memories"] == {
        "global": {"own": False, "roles": [BASE], "allow": False},
        "t": {"own": True, "roles": [], "allow": False},
        "team": {"own": False, "roles": ["triage"], "allow": False},
    }
    assert {m["name"]: m["editable"] for m in got["memories"]} == {"global": False, "t": True, "team": True}
    assert got["secrets"] == [
        {
            "name": "K",
            "owner": None,
            "description": "a key",
            "source": {"own": False, "roles": [], "allow": True},
            "editable": False,
        }
    ]
    assert "secret-value" not in str(got)
    assert [(m["name"], m["transport"], m["enabled"]) for m in got["mcp"]] == [("echo", "http", True)]
    # 记忆预览就是运行时 instructions 里的那一段
    assert got["memory_preview"] == memory_section(
        await container.memory.for_run(agent.id), container.memory.max_chars
    )
    assert '<memory name="team" access="editable, shared"' in got["memory_preview"]
    assert "\n- team fact\n</memory>" in got["memory_preview"]
    # 与 reach（运行时同一套规则）列出的资产一致
    reach = await container.distribution.reach(agent.id)
    listed = [
        f"{k}:{a['name']}"
        for k, g in (("mcp", "mcp"), ("memory", "memories"), ("secret", "secrets"), ("skill", "skills"))
        for a in got[g]
    ]
    assert sorted(listed) == sorted(reach.assets)
    assert (await client.get("/api/agents/nope/assets")).status_code == 404

    # admin 靠 write:* 能改全局记忆，但不算来源（use 只来自 base）
    await container.agents.update(agent.id, AgentPatch(roles=["triage", ADMIN]), actor="user")
    got = (await client.get(f"/api/agents/{agent.id}/assets")).json()
    memories = {m["name"]: m for m in got["memories"]}
    assert memories["global"]["editable"] and memories["global"]["source"]["roles"] == [BASE]
    assert 'name="global" access="editable, shared"' in got["memory_preview"]


def test_every_stored_kind_is_a_kind():
    assert {as_kind(k) for k in StoredKind} == set(Kind) - {Kind.TOOL, Kind.PATH}


async def test_an_item_denied_to_every_other_agent_goes_with_its_owner(container, make_agent):
    owner, other = await make_agent("owner"), await make_agent("other")
    await container.skills.write("shared", "d", "b", version=None, owner=owner.id)
    await container.skills.write("public", "d", "b", version=None, owner=owner.id)
    await container.distribution.create(RoleSpec("triage", "t", ["use:skill:shared"], []))
    patch = AgentPatch(roles=["triage"], deny=["use:skill:shared", "use:skill:public"])
    await container.agents.update(other.id, patch, actor="user")
    await container.distribution.patch(BASE, allow=["use:skill:public"], deny=[])
    # 资产页与删除用同一个「收录」：triage 收录它，但带 triage 的唯一一个 agent 拦了它，没人在用
    [entry] = [e for e in await container.catalog.catalog() if e.name == "shared"]
    assert entry.roles == ("triage",)
    await container.agent_lifecycle.delete(owner.id, actor="user")
    await drain(container)
    # base 收录的照样转给平台：它也给以后新建的 agent
    assert await owned(container, "skill", None) == ["public"]
