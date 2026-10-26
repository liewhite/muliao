"""分发与归属（#235）：`kind:name` 规则、内置角色 base / admin、自建角色；能用 = 自己的 ∪ 规则放行的（skill 索引、MCP、
secret）；能改 = owner 或 admin；read_skill 的软隔离。"""

import sys

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import McpTransport, RoleSpec, Rules, mcp_config
from muliao.agents.permissions import ADMIN, BASE, FIXED_RULES, TOOLS, Kind, check_patterns
from muliao.agents.service import AgentPatch, AgentSpec
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.sessions.api import Origin
from tests.conftest import login, owned
from tests.scripted import scripted
from tests.test_flow import drain, start
from tests.test_triggers import ECHO_SERVER

# 管理类工具：base 没有，admin 有
_ADMIN_TOOLS = {
    "memory_edit_public",
    "create_agent",
    "update_agent",
    "delete_agent",
    "create_role",
    "update_role",
    "delete_role",
}


def test_rules_match_kind_and_name_and_deny_wins():
    rules = Rules(allow=("tool:*", "skill:relay-*", "secret:RELAY_*"), deny=("tool:delete_agent",))
    assert rules.allows(Kind.TOOL, "create_agent") and not rules.allows(Kind.TOOL, "delete_agent")
    assert rules.allows(Kind.SKILL, "relay-host") and not rules.allows(Kind.SKILL, "other")
    assert rules.allows(Kind.SECRET, "RELAY_KEY") and not rules.allows(Kind.MCP, "RELAY_KEY")
    # 种类不串：skill 规则不放行同名 MCP
    assert not Rules(allow=("skill:*",)).allows(Kind.MCP, "x")
    union = Rules.union(
        [Rules(allow=("tool:issue_*",)), Rules(allow=("tool:*",), deny=("tool:issue_update",))]
    )
    assert union.allows(Kind.TOOL, "issue_create") and not union.allows(Kind.TOOL, "issue_update")


def test_patterns_need_a_kind_and_tool_patterns_a_tool():
    check_patterns(["tool:*", "tool:issue_*", "skill:anything", "mcp:x", "secret:K_*"])
    for bad in ("issue:list", "*", "tool:", "skills:x", ":x"):
        with pytest.raises(InvalidInput, match="kind:name"):
            check_patterns([bad])
    with pytest.raises(InvalidInput, match="match no tool"):
        check_patterns(["tool:isue_*"])
    with pytest.raises(InvalidInput, match="distinct"):
        check_patterns(["tool:issue_*", "tool:issue_*"])


def test_base_has_every_tool_but_admin_ones_and_admin_has_all():
    every = list(TOOLS)
    assert list(FIXED_RULES) == [BASE, ADMIN]
    assert FIXED_RULES[ADMIN].tools(every) == every
    assert set(every) - set(FIXED_RULES[BASE].tools(every)) == _ADMIN_TOOLS
    assert {n for n, t in TOOLS.items() if t.admin} == _ADMIN_TOOLS
    # base 是纯 allow：admin 角色或自己的 allow 能放开管理工具，不被它压住
    assert not FIXED_RULES[BASE].deny


async def test_new_agents_have_base_implicitly(container):
    agent = await container.agents.create(AgentSpec(name="a", description="d"), author="user")
    assert agent.roles == ()
    tools = container.agents.resolve(await container.agents.rules(agent))
    assert not set(tools) & _ADMIN_TOOLS and {"run_shell", "memory_edit", "vault_set"} <= set(tools)
    with pytest.raises(InvalidInput, match="implicit"):
        await container.agents.create(AgentSpec(name="b", description="d", roles=[BASE]), author="user")
    admin = await container.agents.create(AgentSpec(name="c", description="d", roles=[ADMIN]), author="user")
    assert container.agents.resolve(await container.agents.rules(admin)) == list(container.agents.tools)


async def test_roles_are_created_assigned_updated_and_protected(container):
    roles = container.roles
    await roles.create(RoleSpec("reviewer", "reads issues", ["tool:issue_*"], ["tool:issue_update"]))
    with pytest.raises(Conflict):
        await roles.create(RoleSpec("reviewer", "again", ["tool:issue_*"], []))
    with pytest.raises(Conflict):
        await roles.create(RoleSpec("admin", "shadow a built-in", ["tool:*"], []))
    with pytest.raises(InvalidInput, match="match no tool"):
        await roles.create(RoleSpec("typo", "d", ["tool:isue_*"], []))
    with pytest.raises(InvalidInput, match="no existing"):
        await roles.create(RoleSpec("typo", "d", ["skill:nope"], []))
    with pytest.raises(InvalidInput, match="built in"):
        await roles.delete(BASE)
    with pytest.raises(NotFound):
        await roles.delete("nope")

    agent = await container.agents.create(
        AgentSpec(name="r", description="d", deny=["tool:*"], roles=["reviewer"]), author="user"
    )
    # 自己的 deny 盖过 base 与角色
    assert container.agents.resolve(await container.agents.rules(agent)) == []
    agent = await container.agents.update(agent.id, AgentPatch(deny=["tool:run_shell"]))
    assert "issue_update" not in container.agents.resolve(await container.agents.rules(agent))

    with pytest.raises(Conflict, match="used by agents"):
        await roles.delete("reviewer")
    await container.agents.update(agent.id, AgentPatch(roles=[]))
    await roles.delete("reviewer")
    assert [r.name for r in await roles.list()] == [BASE, ADMIN]
    with pytest.raises(InvalidInput, match="roles must be"):
        await container.agents.create(AgentSpec(name="x", description="d", roles=["public"]), author="user")


async def test_a_role_hands_out_skills_and_secrets_and_an_items_owner_always_has_it(container, make_agent):
    owner, trader, other = await make_agent("owner"), await make_agent("trader"), await make_agent("other")
    await container.skills.write("relay-host", "d", "body", actor=owner.id, owner=owner.id)
    await container.vault.set("RELAY_KEY", "v", "", actor=owner.id, owner=owner.id)
    assert [s.name for s in await container.skills.index(owner.id)] == ["relay-host"]
    assert await container.skills.index(trader.id) == []

    await container.roles.create(
        RoleSpec("relay-trader", "relay", ["skill:relay-host", "secret:RELAY_*"], [])
    )
    await container.agents.update(trader.id, AgentPatch(roles=["relay-trader"]))
    assert [s.name for s in await container.skills.index(trader.id)] == ["relay-host"]
    assert await container.vault.resolve_many(trader.id, ["RELAY_KEY"]) == {"RELAY_KEY": "v"}
    assert await container.agents.usable_items(trader.id) == ["secret:RELAY_KEY", "skill:relay-host"]
    with pytest.raises(NotFound):
        await container.vault.resolve_many(other.id, ["RELAY_KEY"])

    # deny 只作用于规则分来的，不作用于自己的
    await container.agents.update(owner.id, AgentPatch(deny=["skill:*"]))
    assert [s.name for s in await container.skills.index(owner.id)] == ["relay-host"]
    await container.agents.update(trader.id, AgentPatch(deny=["skill:*"]))
    assert await container.skills.index(trader.id) == []

    # 软隔离：没分到的照样读得到，带提示标记
    skill = await container.skills.resolve(other.id, "relay-host")
    assert skill.body.strip() == "body" and not skill.usable


async def test_only_the_owner_an_admin_or_the_user_can_change_an_item(container, make_agent):
    owner, stranger = await make_agent("owner"), await make_agent("stranger")
    admin = await make_agent("boss", roles=(ADMIN,))
    await container.skills.write("s", "d", "b", actor=owner.id, owner=owner.id)
    await container.vault.set("K", "v", "", actor=owner.id, owner=owner.id)
    with pytest.raises(Forbidden, match="belongs to agent 'owner'"):
        await container.skills.write("s", "d", "steal", actor=stranger.id, owner=stranger.id)
    with pytest.raises(Forbidden):
        await container.skills.edit("s", stranger.id, new="x")
    with pytest.raises(Forbidden):
        await container.skills.delete("s", stranger.id)
    with pytest.raises(Forbidden):
        await container.vault.set("K", "x", "", actor=stranger.id, owner=stranger.id)
    with pytest.raises(Forbidden):
        await container.vault.delete("K", stranger.id)
    # admin 改别人的：owner 不变
    meta = await container.skills.edit("s", admin.id, new="by admin")
    assert meta.owner == owner.id
    await container.skills.write("p", "d", "b", actor="user", owner=None)
    with pytest.raises(Forbidden, match="belongs to the platform"):
        await container.skills.delete("p", owner.id)
    await container.skills.delete("p", admin.id)
    await container.vault.delete("K", "user")


async def test_a_deleted_agents_shared_items_move_to_the_platform(container, make_agent):
    owner, user = await make_agent("owner"), await make_agent("user1")
    await container.skills.write("shared", "d", "b", actor=owner.id, owner=owner.id)
    await container.skills.write("alone", "d", "b", actor=owner.id, owner=owner.id)
    base = await container.roles.get(BASE)
    await container.roles.update(RoleSpec(BASE, base.description, ["skill:shared"], []))
    await container.agent_lifecycle.delete(owner.id, author="user")
    await drain(container)
    assert await owned(container, "skill", None) == ["shared"]
    assert [s.name for s in await container.skills.index(user.id)] == ["shared"]
    assert (container.skills.dir("shared") / "SKILL.md").exists()
    assert not container.skills.dir("alone").exists()


async def test_mcp_is_what_the_agent_owns_or_its_roles_give_it(container, models):
    spec = mcp_config(transport=McpTransport.STDIO, command=sys.executable, args=[ECHO_SERVER])
    await container.mcp.add("echo", spec, actor="user", owner=None, probe=False)
    await container.mcp.add("other", spec, actor="user", owner=None, probe=False)
    await container.roles.create(RoleSpec("echoer", "echo", ["mcp:echo"], []))
    picky = await container.agents.create(
        AgentSpec(name="picky", description="d", model="m1", roles=["echoer"]), author="user"
    )
    await container.mcp.add("mine", spec, actor="user", owner=picky.id, probe=False)
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
    # 停用的不连接
    await container.mcp.set_enabled("echo", False, "user")
    assert [s.name for s in await container.mcp.index(picky.id)] == ["mine"]


async def test_an_item_rule_survives_its_item_being_removed(container):
    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add("echo", spec, actor="user", owner=None, probe=False)
    await container.roles.create(RoleSpec("echoer", "echo", ["mcp:echo"], []))
    await container.mcp.remove("echo", "user")
    # 已有的规则不查是否匹配现有条目：改描述照常
    await container.roles.update(RoleSpec("echoer", "new", ["mcp:echo"], []))
    with pytest.raises(InvalidInput, match="no existing"):
        await container.roles.update(RoleSpec("echoer", "new", ["mcp:echo", "mcp:gone"], []))


async def test_tools_and_roles_api(client, container):
    await login(client)
    tools = (await client.get("/api/tools")).json()
    assert {"name": "delete_agent", "access": "write", "admin": True} in tools

    def preview(agent_id=None, roles=(), allow=(), deny=()):
        body = {"agent_id": agent_id, "roles": list(roles), "allow": list(allow), "deny": list(deny)}
        return client.post("/api/agent-preview", json=body)

    resolved = (await preview(deny=["tool:memory_*"])).json()
    names = [t["name"] for t in resolved["tools"]]
    assert "run_shell" in names and "memory_edit" not in names and "create_agent" not in names
    assert (await preview(roles=["nope"])).status_code == 422
    assert (await preview(allow=["skill:nope"])).status_code == 422

    await container.skills.write("s", "d", "b", actor="user", owner=None)
    body = {"description": "issues only", "allow": ["tool:issue_*", "skill:s"], "deny": []}
    role_preview = (
        await client.post("/api/role-preview", json={"name": None, "allow": body["allow"], "deny": []})
    ).json()
    assert role_preview["items"] == ["skill:s"]
    assert [t["name"] for t in role_preview["tools"]][:2] == ["issue_list", "issue_show"]
    bad_role = {"name": None, "allow": ["skill:nope"], "deny": []}
    assert (await client.post("/api/role-preview", json=bad_role)).status_code == 422
    assert (await client.post("/api/roles/triage", json=body)).status_code == 201
    listed = {r["name"]: r for r in (await client.get("/api/roles")).json()}
    assert listed[BASE]["builtin"] and not listed["triage"]["builtin"]
    assert "tool:run_shell" in listed[BASE]["fixed_allow"]
    assert [t["name"] for t in listed["triage"]["reach"]["tools"]][:2] == ["issue_list", "issue_show"]
    assert listed["triage"]["reach"]["items"] == ["skill:s"] and listed[BASE]["reach"]["items"] == []
    # 内置角色的预览含它固定的部分
    base_preview = (
        await client.post("/api/role-preview", json={"name": BASE, "allow": [], "deny": []})
    ).json()
    assert "run_shell" in [t["name"] for t in base_preview["tools"]]
    # 内置角色可编辑的部分可以改，不能删
    assert (await client.put("/api/roles/admin", json=body)).status_code == 200
    assert (await client.delete("/api/roles/admin")).status_code == 422

    agent = (
        await client.post("/api/agents", json={"name": "t", "description": "d", "roles": ["triage"]})
    ).json()
    assert (agent["roles"], agent["allow"], agent["deny"]) == (["triage"], [], [])
    listed = {r["name"]: r for r in (await client.get("/api/roles")).json()}
    assert listed["triage"]["agents"] == [agent["id"]] and agent["id"] in listed[BASE]["agents"]
    assert listed["admin"]["agents"] == []
    # 自己的条目总能用，deny 只拦规则分来的
    await container.skills.write("own", "d", "b", actor="user", owner=agent["id"])
    reach = (await preview(agent["id"], ["triage"], deny=["skill:*"])).json()
    assert reach["items"] == ["skill:own"]
    assert (await preview(None, ["triage"])).json()["items"] == ["skill:s"]
    assert (await client.delete("/api/roles/triage")).status_code == 409
    assert (await client.delete(f"/api/agents/{agent['id']}")).status_code == 204
    # 删除 agent 的清理做完、行删掉之后角色才没人引用
    await drain(container)
    assert (await client.delete("/api/roles/triage")).status_code == 204
