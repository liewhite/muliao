"""AI 资源存成文件（#498）：资源目录树的读法（归属看目录、坏文件跳过并报出、同名冲突：与平台同名只跳过副本、agent 之间同名全部跳过）、agent 配置只认 agent.toml、
平台自己的读写（界面、vault_set、建 / 改名 / 删 agent）。"""

import json
import os
import tomllib
from pathlib import Path

import pytest
import tomlkit
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo

from muliao.agents.api import McpTransport, RoleSpec, StoredKind, ToolTable, mcp_config
from muliao.agents.assets import MemoryText, content_of, load_tree
from muliao.agents.service import AgentPatch, AgentSpec
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from muliao.platform.tables import TaskStatus
from muliao.sessions.lanes import RUN_TASK
from tests.conftest import login, owned
from tests.scripted import scripted
from tests.test_flow import drain, start, tasks_of


def root(container) -> Path:
    return container.settings.assets_dir


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def skill(directory: Path, description: str = "When testing", body: str = "Steps.") -> None:
    write(directory / "SKILL.md", f"---\ndescription: {description}\n---\n{body}\n")


async def problems(container) -> list[str]:
    snapshot = await container.assets.snapshot()
    return [f"{p.path.relative_to(snapshot.root)}: {p.reason}" for p in snapshot.problems]


# ---- 读树 ----


async def test_assets_belong_to_the_directory_they_are_in(container, make_agent):
    alpha = await make_agent("alpha")
    r = root(container)
    skill(r / "alpha" / "skills" / "mine")
    skill(r / "platform" / "skills" / "shared")
    write(r / "alpha" / "memory.md", "- likes tea\n")
    write(r / "platform" / "memory" / "trading.md", "- fees matter")
    write(r / "alpha" / "mcp" / "docs.json", json.dumps({"transport": "http", "url": "https://x/mcp"}))
    snapshot = await container.assets.snapshot()
    assert snapshot.asset(StoredKind.SKILL, "mine").owner == alpha.id
    assert snapshot.asset(StoredKind.SKILL, "shared").owner is None
    private = snapshot.private_memory(alpha.id)
    assert private is not None and private.content == MemoryText("- likes tea")
    # 私有记忆按所属 agent 认：不占记忆的名字，规则里没有它的名字
    assert snapshot.get(StoredKind.MEMORY, "alpha") is None and private.rule_name is None
    assert snapshot.asset(StoredKind.MEMORY, "trading").owner is None
    server = await container.mcp.get("docs")
    assert (server.owner, server.enabled, server.config.url) == (alpha.id, True, "https://x/mcp")
    assert await problems(container) == []


async def test_broken_files_are_skipped_and_reported_to_their_owner(container, make_agent):
    alpha = await make_agent("alpha")
    r = root(container)
    write(r / "alpha" / "skills" / "nodesc" / "SKILL.md", "---\nname: nodesc\n---\nbody\n")
    write(r / "alpha" / "mcp" / "bad.json", "{not json")
    write(r / "alpha" / "secrets" / "TOKEN.toml", 'description = "x"\n')
    write(r / "alpha" / "notes.txt", "stray")
    write(r / "platform" / "memory" / "notes.txt", "x")
    snapshot = await container.assets.snapshot()
    assert snapshot.get(StoredKind.SKILL, "nodesc") is None
    assert snapshot.get(StoredKind.MCP, "bad") is None
    assert snapshot.get(StoredKind.SECRET, "TOKEN") is None
    found = {(str(p.path.relative_to(r)), p.owner) for p in snapshot.problems}
    assert found == {
        ("alpha/skills/nodesc", alpha.id),
        ("alpha/mcp/bad.json", alpha.id),
        ("alpha/secrets/TOKEN.toml", alpha.id),
        ("alpha/notes.txt", alpha.id),
        ("platform/memory/notes.txt", None),
    }
    beta = await make_agent("beta")
    boss = await make_agent("boss", roles=("admin",))
    snapshot = await container.assets.snapshot()
    # 报给相关的：自己目录里的；平台的只报给 admin
    assert snapshot.problems_for(beta.id) == []
    assert {str(p.path.relative_to(r)) for p in snapshot.problems_for(boss.id)} == {
        "platform/memory/notes.txt"
    }


async def test_a_name_used_by_two_agents_loads_neither_and_reports_both(container, make_agent):
    alpha = await make_agent("alpha")
    beta = await make_agent("beta")
    r = root(container)
    skill(r / "alpha" / "skills" / "dup")
    skill(r / "beta" / "skills" / "dup")
    snapshot = await container.assets.snapshot()
    assert snapshot.get(StoredKind.SKILL, "dup") is None
    assert sorted((str(p.path.relative_to(r)), p.owner) for p in snapshot.problems) == sorted(
        [("alpha/skills/dup", alpha.id), ("beta/skills/dup", beta.id)]
    )
    with pytest.raises(Conflict, match="not loaded"):
        await container.skills.write("dup", "When testing", "x", version=None, owner=None)


async def test_a_copy_of_a_platform_asset_breaks_only_the_copy(container, make_agent):
    # #502：agent 在自己目录里复制一份公共资源，平台那份照常加载，只有副本不加载、报给这个 agent
    alpha = await make_agent("alpha")
    r = root(container)
    skill(r / "platform" / "skills" / "dup", description="Platform one")
    skill(r / "alpha" / "skills" / "dup", description="Copy")
    write(r / "platform" / "mcp" / "docs.json", json.dumps({"transport": "http", "url": "https://x/mcp"}))
    write(r / "alpha" / "mcp" / "docs.json", json.dumps({"transport": "http", "url": "https://y/mcp"}))
    snapshot = await container.assets.snapshot()
    kept = snapshot.asset(StoredKind.SKILL, "dup")
    assert (kept.owner, kept.description) == (None, "Platform one")
    assert snapshot.asset(StoredKind.MCP, "docs").owner is None
    reported = {(str(p.path.relative_to(r)), p.owner): p.reason for p in snapshot.problems}
    assert set(reported) == {("alpha/skills/dup", alpha.id), ("alpha/mcp/docs.json", alpha.id)}
    assert "platform/skills/dup" in reported[("alpha/skills/dup", alpha.id)]
    assert len(snapshot.problems_for(alpha.id)) == 2


async def test_a_platform_asset_with_hidden_copies_is_not_transferred_to_an_agent(container, make_agent):
    # 转给 agent 后副本与它就是 agent 之间同名，两边都不加载：先拒绝，说清副本在哪
    await make_agent("alpha")
    beta = await make_agent("beta")
    r = root(container)
    skill(r / "platform" / "skills" / "dup")
    skill(r / "alpha" / "skills" / "dup")
    with pytest.raises(Conflict, match="alpha/skills/dup"):
        await container.catalog.transfer(StoredKind.SKILL, "dup", beta.id)
    assert (await container.assets.snapshot()).asset(StoredKind.SKILL, "dup").owner is None


async def test_a_broken_platform_asset_still_hides_agent_copies(container, make_agent):
    alpha = await make_agent("alpha")
    r = root(container)
    write(r / "platform" / "skills" / "dup" / "SKILL.md", "no frontmatter\n")
    skill(r / "alpha" / "skills" / "dup")
    snapshot = await container.assets.snapshot()
    assert snapshot.get(StoredKind.SKILL, "dup") is None
    reported = {(str(p.path.relative_to(r)), p.owner) for p in snapshot.problems}
    assert reported == {("platform/skills/dup", None), ("alpha/skills/dup", alpha.id)}


async def test_a_platform_memory_named_like_an_agent_does_not_touch_its_private_memory(container, make_agent):
    # 私有记忆不占记忆的名字（#635）：同名的平台记忆与它各自加载，规则 `memory:alpha` 只指平台那份
    alpha = await make_agent("alpha")
    beta = await make_agent("beta")
    r = root(container)
    write(r / "alpha" / "memory.md", "mine\n")
    write(r / "platform" / "memory" / "alpha.md", "platform's\n")
    await container.agents.update(beta.id, AgentPatch(allow=["use:memory:alpha"]), actor="user")
    snapshot = await container.assets.snapshot()
    assert snapshot.asset(StoredKind.MEMORY, "alpha").owner is None
    assert snapshot.private_memory(alpha.id).content == MemoryText("mine")
    assert await problems(container) == []
    assert [(m.content, m.yours) for m in await container.memory.for_run(beta.id) if m.name == "alpha"] == [
        ("platform's", False)
    ]
    alphas = [(m.content, m.yours) for m in await container.memory.for_run(alpha.id) if m.name == "alpha"]
    assert alphas == [("mine", True)]


async def test_an_agent_directory_must_carry_a_known_id_under_the_agents_own_name(container, make_agent):
    alpha = await make_agent("alpha")
    beta = await make_agent("beta")
    r = root(container)
    write(r / "ghost" / "agent.toml", 'id = "0123456789ab"\ndescription = "d"\nmodel = "test"\n')
    # 名字只认数据库：手动 mv 的目录不加载，不猜是改名（也不会撞上别的 agent 占着的名字）
    (r / "beta").rename(r / "gamma")
    (r / "alpha").rename(r / "beta")
    snapshot = await container.assets.snapshot()
    assert snapshot.agents == {}
    reported = {(str(p.path.relative_to(r)), p.owner): p.reason for p in snapshot.problems}
    assert ("ghost/agent.toml", None) in reported
    assert "move it back to 'alpha'" in reported[("beta", alpha.id)]
    assert "move it back to 'beta'" in reported[("gamma", beta.id)]
    # 运行照常：名字仍是数据库里的
    assert (await container.agents.find_by_name("alpha")).id == alpha.id
    assert [m.name for m in await container.memory.for_run(alpha.id)] == ["global"]


async def test_renaming_in_the_app_moves_the_directory_and_rules_together(container, make_agent):
    await container.distribution.create(RoleSpec("reader", "r", [], []))
    agent = await container.agents.create(
        AgentSpec(name="alpha", description="d", model="test", roles=["reader"]), actor="user"
    )
    await make_agent("beta")
    # 规则不按名字指私有记忆：指名它的新规则匹配不上任何资源，写入时就拒绝
    with pytest.raises(InvalidInput, match="private memories belong to their agent"):
        await container.distribution.create(RoleSpec("peek", "p", ["use:memory:alpha"], []))
    r = root(container)
    with pytest.raises(Conflict, match="already taken"):
        await container.agents.update(agent.id, AgentPatch(name="beta"), actor="user")
    assert (r / "alpha").is_dir()
    renamed = await container.agents.update(agent.id, AgentPatch(name="gamma"), actor="user")
    assert renamed.name == "gamma" and (r / "gamma" / "agent.toml").exists()
    snapshot = await container.assets.snapshot()
    assert snapshot.rules_of(agent.id).roles == ("reader",)
    memory = await container.memory.read_private(agent.id)
    assert (memory.name, memory.owner) == ("gamma", agent.id)
    assert "[agents.gamma]" in (r / "platform" / "access.toml").read_text()
    assert await problems(container) == []


async def test_a_failed_rename_puts_the_files_back(container, make_agent, monkeypatch):
    from muliao.agents import access_file

    agent = await make_agent("alpha")
    r = root(container)
    access = r / "platform" / "access.toml"
    before = access.read_text() if access.exists() else None

    def broken(self):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(access_file.AccessEdit, "apply", broken)
    with pytest.raises(OSError):
        await container.agents.update(agent.id, AgentPatch(name="gamma", description="x"), actor="user")
    assert (r / "alpha" / "agent.toml").exists() and not (r / "gamma").exists()
    assert 'description = "test agent"' in (r / "alpha" / "agent.toml").read_text()
    assert (access.read_text() if access.exists() else None) == before
    assert (await container.agents.get(agent.id)).name == "alpha"


async def test_a_failed_create_takes_back_its_directory_and_rules(container, monkeypatch):
    """access.toml 已写入、之后的一步失败：目录删掉、access.toml 改回原文，同名的还能再建。"""
    r = root(container)
    access = r / "platform" / "access.toml"
    before = access.read_text() if access.exists() else None
    workspace = container.agents.workspace

    def broken(agent_id):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(container.agents, "workspace", broken)
    spec = AgentSpec(name="alpha", description="d", model="m", roles=["admin"])
    with pytest.raises(OSError):
        await container.agents.create(spec, actor="user")
    assert not (r / "alpha").exists()
    assert (access.read_text() if access.exists() else None) == before
    monkeypatch.setattr(container.agents, "workspace", workspace)
    assert (await container.agents.create(spec, actor="user")).name == "alpha"


async def test_an_access_edit_does_not_overwrite_a_change_made_after_it_was_planned(container, tmp_path):
    from muliao.agents import access_file

    snapshot = await container.assets.snapshot()
    path = snapshot.access_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    edit = access_file.plan(snapshot, lambda doc: access_file.delete_role(doc, "x"))
    path.write_text('[roles.ops]\ndescription = "by hand"\n')
    with pytest.raises(Conflict, match="changed meanwhile"):
        edit.apply()
    edit.revert()
    assert "by hand" in path.read_text()


async def test_the_global_memory_cannot_be_deleted(container):
    await container.assets.ensure_global_memory()
    with pytest.raises(InvalidInput, match="cannot be deleted"):
        await container.catalog.delete(StoredKind.MEMORY, "global")
    assert (root(container) / "platform" / "memory" / "global.md").exists()


async def test_a_name_on_disk_but_not_loaded_cannot_be_created_again(container, make_agent):
    await make_agent("alpha")
    write(
        root(container) / "alpha" / "skills" / "digest" / "SKILL.md",
        "---\nname: digest\n---\nno description\n",
    )
    with pytest.raises(Conflict, match="not loaded"):
        await container.skills.write("digest", "When digesting", "x", version=None, owner=None)


async def test_editing_agent_toml_takes_effect_and_a_broken_one_fails_the_run(container, make_agent, models):
    """agent 的配置只认 agent.toml（#549）：改了下次读到就生效；读坏了整个目录不加载，它的运行当场失败，不沿用旧值。"""
    alpha = await make_agent("alpha")
    path = root(container) / "alpha" / "agent.toml"
    write(path, path.read_text().replace('description = "test agent"', 'description = "new role"'))
    view = await container.agents.view(alpha.id)
    assert view.config is not None and view.config.file.description == "new role"

    write(path, path.read_text().replace('thinking = "default"', 'thinking = "extreme"'))
    snapshot = await container.assets.snapshot()
    assert snapshot.get(StoredKind.MEMORY, "alpha") is None  # 整个目录没加载
    assert any("agent.toml" in str(p.path) for p in snapshot.problems)
    assert (await container.agents.view(alpha.id)).config is None
    session = await start(container, alpha.id, "hi")
    await drain(container)
    [turn] = [t for t in await tasks_of(container, RUN_TASK) if t.session_id == session.id]
    assert turn.status == TaskStatus.FAILED and "no loadable directory" in (turn.error or "")


async def test_access_toml_references_to_missing_things_only_narrow(container, make_agent):
    """只引用了不存在的东西（没加载的 agent、匹配不上任何工具的规则）与写了隐含的 base，记问题、其余照常（#573）。"""
    alpha = await make_agent("alpha")
    skill(root(container) / "platform" / "skills" / "digest")
    write(
        root(container) / "platform" / "access.toml",
        """
[roles.reader]
description = "reads"
allow = ["use:skill:digest", "use:tool:no_such_tool"]

[agents.alpha]
roles = ["reader", "base"]

[agents.nobody]
roles = []
""",
    )
    snapshot = await container.assets.snapshot()
    assert snapshot.rules_of(alpha.id).roles == ("reader",)
    assert len(snapshot.problems) == 3
    assert [s.name for s in await container.skills.index(alpha.id)] == ["digest"]


def _drop_watcher(doc: dict) -> None:
    del doc["roles"]["watcher"]


def _drop_alpha(doc: dict) -> None:
    del doc["agents"]["alpha"]


def _alpha_typo(doc: dict) -> None:
    doc["agents"]["alpha"]["roles"] = ["watchr"]


def _roles_not_a_table(doc: dict) -> None:
    doc["roles"] = []
    doc["agents"]["alpha"]["roles"] = []


@pytest.mark.parametrize(
    ("mutate", "extra"),
    [
        (
            _drop_watcher,
            '[roles.watcher]\ndescription = "w"\ndeny = ["use:tool:run_shell"]\nallw = []\n',
        ),  # 键拼错
        (
            _drop_watcher,
            '[roles.watcher]\ndescription = "w"\ndeny = ["use:tool:run_shell", "bogus"]\n',
        ),  # deny 写错一条
        (_drop_alpha, '[agents.alpha]\nroles = ["watcher"]\nrole = []\n'),  # agent 一节键拼错
        (_alpha_typo, ""),  # 引用的角色名拼错：跳过会丢掉它的 deny
        (_roles_not_a_table, ""),  # roles 不是表
        (lambda doc: None, "[other]\n"),  # 不认识的顶层表
        (lambda doc: None, '[roles."Bad Name"]\ndescription = "x"\n'),  # 角色名不合法
        (lambda doc: None, '[roles.empty]\ndescription = " "\n'),  # 角色描述为空
        (lambda doc: None, '[agents.ghost]\ndeny = ["bogus"]\n'),  # 没加载的 agent 那一节里的规则写坏
    ],
)
async def test_any_malformed_part_invalidates_the_whole_access_toml(container, make_agent, mutate, extra):
    """形式上任何一处不合法整份作废、沿用上份规则（#573 延伸 #550）：跳过写坏的那一条（尤其 deny）等于放宽权限。"""
    boss = await make_agent("boss", roles=("admin",))
    await container.distribution.create(RoleSpec("watcher", "w", [], ["use:tool:run_shell"]))
    alpha = await make_agent("alpha", roles=("watcher",))
    path = root(container) / "platform" / "access.toml"
    before = await container.assets.snapshot()
    doc = tomllib.loads(path.read_text())
    mutate(doc)
    write(path, tomlkit.dumps(doc) + "\n" + extra)
    snapshot = await container.assets.snapshot()
    assert snapshot.administers(boss.id)
    assert snapshot.rules_of(alpha.id) == before.rules_of(alpha.id)
    assert snapshot.roles["watcher"] == before.roles["watcher"]
    reported = [p.reason for p in snapshot.problems_for(boss.id) if p.path == path]
    assert reported and all("last read successfully stay in effect" in r for r in reported)
    # 平台自己的写入（界面改角色、建 / 改 / 删 agent）在不合法的文件上一律拒绝，不写一半
    with pytest.raises(Conflict, match="fix it first"):
        await container.distribution.create(RoleSpec("other", "o", [], []))
    with pytest.raises(Conflict, match="fix it first"):
        await make_agent("beta")
    assert not (root(container) / "beta").exists()
    with pytest.raises(Conflict, match="fix it first"):
        await container.agents.update(alpha.id, AgentPatch(name="gamma"), actor="user")
    assert (root(container) / "alpha").is_dir() and not (root(container) / "gamma").exists()
    with pytest.raises(Conflict, match="fix it first"):
        await container.agent_lifecycle.delete(alpha.id, actor="user")
    assert (await container.agents.get(alpha.id)).name == "alpha"


async def test_deleting_a_role_still_referenced_by_an_unloaded_agent_is_refused(container, make_agent):
    """平台自己改完的 access.toml 也要合法：没加载的 agent 那一节还引用着的角色删不掉，文件不变。"""
    await container.distribution.create(RoleSpec("watcher", "w", [], ["use:tool:run_shell"]))
    path = root(container) / "platform" / "access.toml"
    write(path, path.read_text() + '\n[agents.ghost]\nroles = ["watcher"]\n')
    before = path.read_text()
    with pytest.raises(InvalidInput, match="not defined"):
        await container.distribution.delete("watcher")
    assert path.read_text() == before


async def test_an_unparsable_access_toml_keeps_the_last_rules_and_reports_to_their_admin(
    container, make_agent
):
    """access.toml 整个读不出来时沿用上次读到的规则（#550）：否则谁都没了 admin，这条问题报不到人，也没人能改回文件。"""
    boss = await make_agent("boss", roles=("admin",))
    await container.distribution.create(RoleSpec("readers", "r", [], []))
    await make_agent("alpha", roles=("readers",))
    assert (await container.assets.snapshot()).administers(boss.id)
    path = root(container) / "platform" / "access.toml"
    good = path.read_text()
    # alpha 的目录也坏着：原文里 [agents.alpha] 那一节本身就有一条问题
    write(root(container) / "alpha" / "agent.toml", "broken")
    write(path, good + "\n[agents.alpha\n")
    snapshot = await container.assets.snapshot()
    assert snapshot.administers(boss.id)
    reported = [p.reason for p in snapshot.problems_for(boss.id) if p.path == path]
    assert len(reported) == 1 and reported[0].startswith("cannot parse")
    assert "last read successfully stay in effect" in reported[0]
    # 坏着时 admin 用文件工具改回原文：读得出来就放行（原文那条单节问题不挡），仍读不出的照样拦
    with pytest.raises(InvalidInput, match="cannot parse"):
        await container.assets.write({path: good + "\n[agents.beta\n"})
    await container.assets.write({path: good})
    snapshot = await container.assets.snapshot()
    assert snapshot.administers(boss.id)
    assert [p.reason for p in snapshot.problems if p.path == path] == [
        "[agents.alpha]: no loaded agent directory has this name"
    ]


def test_an_unparsable_access_toml_without_an_earlier_read_leaves_only_the_fixed_parts(tmp_path):
    write(tmp_path / "platform" / "access.toml", "[agents.alpha\n")
    snapshot = load_tree(tmp_path, {}, frozenset(), 4000, None, ToolTable.of([]))
    assert snapshot.rules == {}
    [problem] = snapshot.problems
    assert "only the fixed parts of built-in roles apply" in problem.reason


async def test_memory_over_the_limit_is_loaded_but_reported(container, make_agent):
    alpha = await make_agent("alpha")
    write(root(container) / "alpha" / "memory.md", "x" * (container.memory.max_chars + 1))
    snapshot = await container.assets.snapshot()
    private = snapshot.private_memory(alpha.id)
    assert private is not None
    assert len(content_of(private, MemoryText).text) == container.memory.max_chars + 1
    assert any("over the limit" in p.reason for p in snapshot.problems)


def test_hidden_entries_are_ignored(tmp_path):
    (tmp_path / ".git").mkdir()
    write(tmp_path / ".DS_Store", "x")
    write(tmp_path / "platform" / "memory" / ".swp", "x")
    snapshot = load_tree(tmp_path, {}, frozenset(), 4000, None, ToolTable.of([]))
    assert snapshot.problems == ()


# ---- 运行时：记忆、skill 索引、问题报给 agent ----


async def test_memory_blocks_skills_and_problems_reach_the_run(container, make_agent, models):
    alpha = (await make_agent("alpha", model="m")).id
    await make_agent("beta")
    r = root(container)
    write(r / "platform" / "memory" / "global.md", "Always answer in Chinese.")
    write(r / "alpha" / "memory.md", "- likes tea")
    write(r / "beta" / "memory.md", "- secret of beta")
    write(r / "platform" / "memory" / "trading.md", "- fees matter")
    skill(r / "alpha" / "skills" / "mine", "When mine")
    write(r / "alpha" / "mcp" / "broken.json", "{")
    await container.distribution.create(RoleSpec("trader", "t", ["use:memory:trading"], []))
    await container.agents.update(alpha, AgentPatch(roles=["trader"]), actor="user")
    seen: list[tuple[str, str]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        first = messages[0].parts[0]
        seen.append((info.instructions or "", str(getattr(first, "content", ""))))
        return ModelResponse(parts=[TextPart("ok")])

    models.models["m"] = scripted(fn)
    await start(container, alpha, "hi")
    await drain(container)

    text, context = seen[0]
    assert (
        f'<memory name="alpha" access="editable, your private memory" file="{r / "alpha" / "memory.md"}">'
        in text
    )
    assert '<memory name="trading" access="read-only, shared"' in text
    assert "secret of beta" not in text
    assert f"- `mine` (yours): When mine ({r / 'alpha' / 'skills' / 'mine' / 'SKILL.md'})" in text
    assert f"yours are in {r / 'alpha'}" in text
    assert "Asset file not loaded" in context and "broken.json" in context


# ---- 平台自己的读写 ----


async def test_memory_api_writes_files_and_follows_ownership(client, container, make_agent):
    agent = await make_agent("alpha")
    await login(client)
    got = (await client.get("/api/assets/memory/global")).json()
    assert (got["content"], got["owner"], got["max_chars"]) == ("", None, 4000)
    resp = await client.put(
        "/api/assets/memory/global", json={"content": "first  \n\n\nsecond", "version": got["version"]}
    )
    assert resp.json()["content"] == "first\n\nsecond"
    assert (root(container) / "platform" / "memory" / "global.md").read_text() == "first\n\nsecond\n"
    # 私有记忆按所属的 agent 认（不在 /assets/memory/<名> 下）
    assert (await client.get("/api/assets/memory/alpha")).status_code == 404
    alpha = (await client.get(f"/api/agents/{agent.id}/memory")).json()
    assert (alpha["name"], alpha["owner"]) == ("alpha", agent.id)
    put = await client.put(
        f"/api/agents/{agent.id}/memory", json={"content": "- tea", "version": alpha["version"]}
    )
    assert put.json()["content"] == "- tea"
    assert (root(container) / "alpha" / "memory.md").read_text() == "- tea\n"
    stale = await client.put(
        f"/api/agents/{agent.id}/memory", json={"content": "x", "version": alpha["version"]}
    )
    assert stale.status_code == 409
    assert (await client.get("/api/agents/nope/memory")).status_code == 404
    created = await client.put("/api/assets/memory/trading", json={"content": "x", "version": None})
    assert created.json()["owner"] is None
    bad = await client.put("/api/assets/memory/Bad Name", json={"content": "x", "version": None})
    assert bad.status_code == 422
    assert (await client.delete("/api/assets/memory/trading")).status_code == 204
    assert not (root(container) / "platform" / "memory" / "trading.md").exists()
    assert (await client.delete("/api/assets/memory/global")).status_code == 422
    assert (await client.get("/api/assets/memory/nope")).status_code == 404
    current = resp.json()["version"]
    over = await client.put("/api/assets/memory/global", json={"content": "x" * 4001, "version": current})
    assert over.status_code == 422


async def test_saving_memory_or_skill_changed_elsewhere_since_opening_is_a_conflict(client, container):
    """界面保存带打开时的版本：期间 agent（直接改文件）或别的页面改过、删掉、抢先建了，都 409，不盖掉别人的改动。"""
    await login(client)
    opened = (await client.get("/api/assets/memory/global")).json()
    path = root(container) / "platform" / "memory" / "global.md"
    path.write_text("- written by an agent\n")
    resp = await client.put(
        "/api/assets/memory/global", json={"content": "mine", "version": opened["version"]}
    )
    assert resp.status_code == 409
    assert path.read_text() == "- written by an agent\n"
    # 新建撞上已有的不再把它清空
    assert (
        await client.put("/api/assets/memory/global", json={"content": "", "version": None})
    ).status_code == 409
    assert path.read_text() == "- written by an agent\n"
    # 重新打开拿到新版本就能存
    again = (await client.get("/api/assets/memory/global")).json()
    resp = await client.put(
        "/api/assets/memory/global", json={"content": "mine", "version": again["version"]}
    )
    assert resp.status_code == 200

    body = {"owner": None, "description": "d", "body": "steps"}
    assert (await client.put("/api/assets/skill/s", json={**body, "version": None})).status_code == 200
    skill = (await client.get("/api/assets/skill/s")).json()
    md = await container.skills.directory("s") / "SKILL.md"
    md.write_text(md.read_text() + "more by an agent\n")
    resp = await client.put("/api/assets/skill/s", json={**body, "version": skill["version"]})
    assert resp.status_code == 409 and "more by an agent" in md.read_text()
    assert (await client.put("/api/assets/skill/s", json={**body, "version": None})).status_code == 409
    fresh = (await client.get("/api/assets/skill/s")).json()["version"]
    assert (await client.put("/api/assets/skill/s", json={**body, "version": fresh})).status_code == 200
    # 打开之后被删掉了
    assert (await client.delete("/api/assets/skill/s")).status_code == 204
    assert (await client.put("/api/assets/skill/s", json={**body, "version": fresh})).status_code == 409


async def test_skills_are_written_and_deleted_as_directories(container, make_agent):
    alpha = await make_agent("alpha")
    meta = await container.skills.write("digest", "When digesting", "Do it.", version=None, owner=alpha.id)
    directory = root(container) / "alpha" / "skills" / "digest"
    assert meta.path == directory / "SKILL.md"
    write(directory / "scripts" / "run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(directory / "scripts" / "run.sh", 0o755)
    # 界面改正文：附件不动，frontmatter 别的键保留
    write(directory / "SKILL.md", "---\nname: digest\ndescription: old\nlicense: MIT\n---\nOld.\n")
    await container.skills.write(
        "digest",
        "When digesting",
        "New.",
        version=(await container.skills.read("digest")).version,
        owner=None,
    )
    text = (directory / "SKILL.md").read_text()
    assert "license: MIT" in text and "New." in text
    assert (directory / "scripts" / "run.sh").exists()
    assert os.access(directory / "scripts" / "run.sh", os.X_OK)
    await container.catalog.delete(StoredKind.SKILL, "digest")
    assert not directory.exists()


async def test_mcp_servers_are_files(container, make_agent):
    alpha = await make_agent("alpha")
    config = mcp_config(transport=McpTransport.HTTP, url="https://x/mcp", headers={"A": "${vault:K}"})
    await container.mcp.add("docs", config, owner=alpha.id, probe=False)
    with pytest.raises(Conflict):
        await container.mcp.add("docs", config, owner=None, probe=False)
    path = root(container) / "alpha" / "mcp" / "docs.json"
    assert json.loads(path.read_text())["headers"] == {"A": "${vault:K}"}
    await container.mcp.set_enabled("docs", False)
    assert json.loads(path.read_text())["enabled"] is False
    assert await container.mcp.index(alpha.id) == []
    await container.catalog.delete(StoredKind.MCP, "docs")
    assert not path.exists()


async def test_secrets_are_encrypted_files(container, make_agent):
    alpha = await make_agent("alpha")
    beta = await make_agent("beta")
    await container.vault.set("TOKEN", "s3cret", "slack", actor=alpha.id, owner=alpha.id)
    path = root(container) / "alpha" / "secrets" / "TOKEN.toml"
    assert "s3cret" not in path.read_text() and 'description = "slack"' in path.read_text()
    assert await container.vault.resolve_many(alpha.id, ["TOKEN"], shell=False) == {"TOKEN": "s3cret"}
    with pytest.raises(NotFound):
        await container.vault.resolve_many(beta.id, ["TOKEN"], shell=False)
    with pytest.raises(Forbidden):
        await container.vault.set("TOKEN", "x", "", actor=beta.id, owner=beta.id)
    write(path, 'description = "slack"\nciphertext = "gAAAAbroken"\n')
    with pytest.raises(InvalidInput, match="cannot be decrypted"):
        await container.vault.resolve_many(alpha.id, ["TOKEN"], shell=False)
    await container.catalog.delete(StoredKind.SECRET, "TOKEN")
    assert not path.exists()


async def test_transferring_moves_the_file_to_the_new_owner(container, make_agent):
    alpha = await make_agent("alpha")
    await container.skills.write("digest", "When digesting", "Do it.", version=None, owner=alpha.id)
    await container.catalog.transfer(StoredKind.SKILL, "digest", None)
    assert (root(container) / "platform" / "skills" / "digest" / "SKILL.md").exists()
    assert await owned(container, "skill", None) == ["digest"]
    with pytest.raises(InvalidInput, match="already belongs"):
        await container.catalog.transfer(StoredKind.SKILL, "digest", None)
    # 记忆不转移：私有记忆属于它的 agent，平台的记忆归了 agent 就是第二块私有记忆
    await container.memory.put("trading", "x", version=None)
    with pytest.raises(InvalidInput, match="not transferred"):
        await container.catalog.transfer(StoredKind.MEMORY, "trading", alpha.id)
    assert (root(container) / "platform" / "memory" / "trading.md").exists()


# ---- agent 的建、改、删 ----


async def test_creating_and_renaming_an_agent_writes_its_directory_and_rules(container, make_agent):
    await container.distribution.create(RoleSpec("reader", "r", [], []))
    agent = await container.agents.create(
        AgentSpec(name="alpha", description="d", model="test", roles=["reader"], allow=["write:path:~/x"]),
        actor="user",
    )
    r = root(container)
    assert f'id = "{agent.id}"' in (r / "alpha" / "agent.toml").read_text()
    assert (r / "alpha" / "memory.md").read_text() == ""
    access = (r / "platform" / "access.toml").read_text()
    assert "[agents.alpha]" in access and '"write:path:~/x"' in access
    with pytest.raises(Conflict):
        await container.agents.create(AgentSpec(name="alpha", description="d"), actor="user")
    for bad in ("platform", "../escape", "a/b", ".hidden"):
        with pytest.raises(InvalidInput, match="directory name"):
            await container.agents.create(AgentSpec(name=bad, description="d"), actor="user")
    assert sorted(p.name for p in r.iterdir()) == ["alpha", "platform"]
    # 私有记忆不占记忆的名字：与全局记忆同名的 agent 也能建，各是各的
    await container.assets.ensure_global_memory()
    named_global = await container.agents.create(AgentSpec(name="global", description="d"), actor="user")
    snapshot = await container.assets.snapshot()
    assert snapshot.asset(StoredKind.MEMORY, "global").owner is None
    assert snapshot.private_memory(named_global.id) is not None

    # 手写的注释保留；改名挪目录，access.toml 里那一节跟着改名
    write(r / "alpha" / "agent.toml", "# keep me\n" + (r / "alpha" / "agent.toml").read_text())
    await container.assets.snapshot()
    updated = await container.agents.update(
        agent.id, AgentPatch(name="beta", description="new"), actor="user"
    )
    view = await container.agents.view(agent.id)
    assert view.config is not None and (updated.name, view.config.file.description) == ("beta", "new")
    assert not (r / "alpha").exists()
    assert (r / "beta" / "agent.toml").read_text().startswith("# keep me\n")
    access = (r / "platform" / "access.toml").read_text()
    assert "[agents.beta]" in access and "[agents.alpha]" not in access
    assert (await container.assets.snapshot()).rules_of(agent.id).roles == ("reader",)


async def test_deleting_an_agent_removes_its_directory_but_keeps_what_others_use(container, make_agent):
    alpha = await make_agent("alpha")
    await container.skills.write("kept", "When kept", "x", version=None, owner=alpha.id)
    await container.skills.write("gone", "When gone", "x", version=None, owner=alpha.id)
    await container.distribution.create(RoleSpec("readers", "r", ["use:skill:kept"], []))
    await make_agent("beta", roles=("readers",))
    await container.agent_lifecycle.delete(alpha.id, actor="user")
    await drain(container)
    r = root(container)
    assert not (r / "alpha").exists()
    assert (r / "platform" / "skills" / "kept" / "SKILL.md").exists()
    assert not (r / "platform" / "skills" / "gone").exists()
    assert "[agents.alpha]" not in (r / "platform" / "access.toml").read_text()


async def test_deleting_an_agent_whose_directory_is_not_loaded_frees_its_name(container, make_agent):
    """目录没加载成功（agent.toml 坏了）也删掉目录与 access.toml 里那一节，同名能再建（#550）。"""
    await container.distribution.create(RoleSpec("readers", "r", [], []))
    alpha = await make_agent("alpha", roles=("readers",))
    r = root(container)
    write(r / "alpha" / "agent.toml", "broken")
    assert alpha.id not in (await container.assets.snapshot()).agents
    await container.agent_lifecycle.delete(alpha.id, actor="user")
    await drain(container)
    assert not (r / "alpha").exists()
    assert "[agents.alpha]" not in (r / "platform" / "access.toml").read_text()
    again = await make_agent("alpha")
    assert again.id in (await container.assets.snapshot()).agents
