"""AI 资源存成文件（#498）：资源目录树的读法（归属看目录、坏文件跳过并报出、同名冲突全部跳过）、agent 配置同步进镜像列、
平台自己的读写（界面、vault_set、建 / 改名 / 删 agent），以及从数据库一次性导出。"""

import json
import os
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo
from sqlalchemy import select

from muliao.agents.api import McpTransport, RoleSpec, StoredKind, mcp_config
from muliao.agents.assets import load_tree
from muliao.agents.export import export_if_missing
from muliao.agents.service import AgentPatch, AgentSpec
from muliao.agents.tables import Agent, AgentRulesRow, Item, ItemFile, RoleRow
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound
from tests.conftest import login, owned
from tests.scripted import scripted
from tests.test_flow import drain, start


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
    assert snapshot.asset(StoredKind.MEMORY, "alpha").text == "- likes tea"
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


async def test_a_name_used_twice_loads_neither_and_reports_both(container, make_agent):
    await make_agent("alpha")
    r = root(container)
    skill(r / "alpha" / "skills" / "dup")
    skill(r / "platform" / "skills" / "dup")
    snapshot = await container.assets.snapshot()
    assert snapshot.get(StoredKind.SKILL, "dup") is None
    assert sorted(str(p.path.relative_to(r)) for p in snapshot.problems) == [
        "alpha/skills/dup",
        "platform/skills/dup",
    ]


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
    await container.distribution.create(RoleSpec("reader", "r", [], []), actor="user")
    agent = await container.agents.create(
        AgentSpec(name="alpha", description="d", model="test", roles=["reader"]), actor="user"
    )
    await make_agent("beta")
    r = root(container)
    with pytest.raises(Conflict, match="already taken"):
        await container.agents.update(agent.id, AgentPatch(name="beta"), actor="user")
    assert (r / "alpha").is_dir()
    renamed = await container.agents.update(agent.id, AgentPatch(name="gamma"), actor="user")
    assert renamed.name == "gamma" and (r / "gamma" / "agent.toml").exists()
    assert (await container.distribution.agent_rules(agent.id)).roles == ("reader",)
    assert (await container.memory.read("gamma")).owner == agent.id
    assert await problems(container) == []


async def test_a_failed_rename_puts_the_files_back(container, make_agent, monkeypatch):
    from muliao.agents import access_file

    agent = await make_agent("alpha")
    r = root(container)
    access = r / "platform" / "access.toml"
    before = access.read_text() if access.exists() else None

    def broken(path, change):  # noqa: ANN001, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(access_file, "edit", broken)
    with pytest.raises(OSError):
        await container.agents.update(agent.id, AgentPatch(name="gamma", description="x"), actor="user")
    assert (r / "alpha" / "agent.toml").exists() and not (r / "gamma").exists()
    assert 'description = "test agent"' in (r / "alpha" / "agent.toml").read_text()
    assert (access.read_text() if access.exists() else None) == before
    assert (await container.agents.get(agent.id)).name == "alpha"


async def test_a_name_on_disk_but_not_loaded_cannot_be_created_again(container, make_agent):
    await make_agent("alpha")
    write(
        root(container) / "alpha" / "skills" / "digest" / "SKILL.md",
        "---\nname: digest\n---\nno description\n",
    )
    with pytest.raises(Conflict, match="not loaded"):
        await container.skills.write("digest", "When digesting", "x", actor="user", owner=None)


async def test_editing_agent_toml_takes_effect_and_an_unrunnable_model_is_reported(
    container, make_agent, models
):
    alpha = await make_agent("alpha")
    path = root(container) / "alpha" / "agent.toml"
    text = path.read_text()
    write(path, text.replace('description = "test agent"', 'description = "new role"'))
    await container.assets.snapshot()
    assert (await container.agents.get(alpha.id)).description == "new role"

    models.models["test"] = scripted(lambda m, i: ModelResponse(parts=[TextPart("ok")]))
    write(path, path.read_text().replace('thinking = "default"', 'thinking = "extreme"'))
    snapshot = await container.assets.snapshot()
    assert snapshot.get(StoredKind.MEMORY, "alpha") is None  # 整个目录没加载
    assert any("agent.toml" in str(p.path) for p in snapshot.problems)
    # 镜像列保留上次的值，照样能运行
    assert (await container.agents.get(alpha.id)).thinking == "default"


async def test_access_toml_skips_only_the_broken_rules(container, make_agent):
    alpha = await make_agent("alpha")
    skill(root(container) / "platform" / "skills" / "digest")
    write(
        root(container) / "platform" / "access.toml",
        """
[roles.reader]
description = "reads"
allow = ["use:skill:digest", "use:tool:no_such_tool"]

[agents.alpha]
roles = ["reader", "missing"]
allow = ["bogus"]

[agents.nobody]
roles = []
""",
    )
    snapshot = await container.assets.snapshot()
    assert snapshot.roles["reader"].rules.allow == ("use:skill:digest",)
    assert snapshot.rules_of(alpha.id).roles == ("reader",)
    assert snapshot.rules_of(alpha.id).allow == ()
    assert len(snapshot.problems) == 4
    assert [s.name for s in await container.skills.index(alpha.id)] == ["digest"]


async def test_memory_over_the_limit_is_loaded_but_reported(container, make_agent):
    await make_agent("alpha")
    write(root(container) / "alpha" / "memory.md", "x" * (container.memory.max_chars + 1))
    snapshot = await container.assets.snapshot()
    assert len(snapshot.asset(StoredKind.MEMORY, "alpha").text) == container.memory.max_chars + 1
    assert any("over the limit" in p.reason for p in snapshot.problems)


def test_hidden_entries_are_ignored(tmp_path):
    (tmp_path / ".git").mkdir()
    write(tmp_path / ".DS_Store", "x")
    write(tmp_path / "platform" / "memory" / ".swp", "x")
    snapshot = load_tree(tmp_path, {}, 4000)
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
    await container.distribution.create(RoleSpec("trader", "t", ["use:memory:trading"], []), actor="user")
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
    got = (await client.get("/api/items/memory/global")).json()
    assert (got["content"], got["owner"], got["max_chars"]) == ("", None, 4000)
    resp = await client.put("/api/items/memory/global", json={"content": "first  \n\n\nsecond"})
    assert resp.json()["content"] == "first\n\nsecond"
    assert (root(container) / "platform" / "memory" / "global.md").read_text() == "first\n\nsecond\n"
    assert (await client.put("/api/items/memory/alpha", json={"content": "- tea"})).json()[
        "owner"
    ] == agent.id
    created = await client.put("/api/items/memory/trading", json={"content": "x"})
    assert created.json()["owner"] is None
    assert (await client.put("/api/items/memory/Bad Name", json={"content": "x"})).status_code == 422
    # 私有记忆名字就是 agent 名
    assert (
        await client.put("/api/items/memory/other", json={"content": "x", "owner": agent.id})
    ).status_code == 422
    assert (await client.delete("/api/items/memory/trading")).status_code == 204
    assert not (root(container) / "platform" / "memory" / "trading.md").exists()
    assert (await client.delete("/api/items/memory/global")).status_code == 422
    assert (await client.get("/api/items/memory/nope")).status_code == 404
    over = await client.put("/api/items/memory/global", json={"content": "x" * 4001})
    assert over.status_code == 422


async def test_writes_need_write_permission(container, make_agent):
    alpha = await make_agent("alpha")
    beta = await make_agent("beta")
    await container.memory.put("trading", "x", actor="user")
    with pytest.raises(Forbidden, match="belongs to the platform"):
        await container.memory.put("trading", "y", actor=alpha.id)
    with pytest.raises(Forbidden, match="agent 'beta'"):
        await container.memory.put("beta", "y", actor=alpha.id)
    await container.distribution.create(RoleSpec("trader", "t", ["*:memory:trading"], []), actor="user")
    await container.agents.update(alpha.id, AgentPatch(roles=["trader"]), actor="user")
    await container.memory.put("trading", "y", actor=alpha.id)
    assert (await container.memory.read("trading")).content == "y"
    await container.memory.put("beta", "own", actor=beta.id)


async def test_skills_are_written_imported_exported_and_deleted_as_directories(container, make_agent):
    alpha = await make_agent("alpha")
    meta = await container.skills.write("digest", "When digesting", "Do it.", actor="user", owner=alpha.id)
    directory = root(container) / "alpha" / "skills" / "digest"
    assert meta.path == directory / "SKILL.md"
    write(directory / "scripts" / "run.sh", "#!/bin/sh\necho hi\n")
    os.chmod(directory / "scripts" / "run.sh", 0o755)
    # 界面改正文：附件不动，frontmatter 别的键保留
    write(directory / "SKILL.md", "---\nname: digest\ndescription: old\nlicense: MIT\n---\nOld.\n")
    await container.skills.write("digest", "When digesting", "New.", actor="user", owner=None)
    text = (directory / "SKILL.md").read_text()
    assert "license: MIT" in text and "New." in text
    assert (directory / "scripts" / "run.sh").exists()
    package = await container.skills.export("digest")
    await container.skills.delete("digest", "user")
    assert not directory.exists()
    imported = await container.skills.import_package(package, actor="user", owner=None, replace=False)
    assert imported.meta.owner is None
    copy = root(container) / "platform" / "skills" / "digest"
    assert os.access(copy / "scripts" / "run.sh", os.X_OK)
    with pytest.raises(Conflict):
        await container.skills.import_package(package, actor="user", owner=None, replace=False)
    await container.skills.import_package(package, actor="user", owner=None, replace=True)
    with pytest.raises(Forbidden):
        await container.skills.delete("digest", alpha.id)


async def test_mcp_servers_are_files(container, make_agent):
    alpha = await make_agent("alpha")
    config = mcp_config(transport=McpTransport.HTTP, url="https://x/mcp", headers={"A": "${vault:K}"})
    await container.mcp.add("docs", config, actor="user", owner=alpha.id, probe=False)
    with pytest.raises(Conflict):
        await container.mcp.add("docs", config, actor="user", owner=None, probe=False)
    path = root(container) / "alpha" / "mcp" / "docs.json"
    assert json.loads(path.read_text())["headers"] == {"A": "${vault:K}"}
    await container.mcp.set_enabled("docs", False, "user")
    assert json.loads(path.read_text())["enabled"] is False
    assert await container.mcp.index(alpha.id) == []
    await container.mcp.remove("docs", "user")
    assert not path.exists()


async def test_secrets_are_encrypted_files(container, make_agent):
    alpha = await make_agent("alpha")
    beta = await make_agent("beta")
    await container.vault.set("TOKEN", "s3cret", "slack", actor=alpha.id, owner=alpha.id)
    path = root(container) / "alpha" / "secrets" / "TOKEN.toml"
    assert "s3cret" not in path.read_text() and 'description = "slack"' in path.read_text()
    assert await container.vault.resolve_many(alpha.id, ["TOKEN"]) == {"TOKEN": "s3cret"}
    with pytest.raises(NotFound):
        await container.vault.resolve_many(beta.id, ["TOKEN"])
    with pytest.raises(Forbidden):
        await container.vault.set("TOKEN", "x", "", actor=beta.id, owner=beta.id)
    write(path, 'description = "slack"\nciphertext = "gAAAAbroken"\n')
    with pytest.raises(InvalidInput, match="cannot be decrypted"):
        await container.vault.resolve_many(alpha.id, ["TOKEN"])
    await container.vault.delete("TOKEN", alpha.id)
    assert not path.exists()


async def test_transferring_moves_the_file_to_the_new_owner(container, make_agent):
    alpha = await make_agent("alpha")
    await container.skills.write("digest", "When digesting", "Do it.", actor="user", owner=alpha.id)
    await container.catalog.transfer_item(StoredKind.SKILL, "digest", None, "user")
    assert (root(container) / "platform" / "skills" / "digest" / "SKILL.md").exists()
    assert await owned(container, "skill", None) == ["digest"]
    with pytest.raises(InvalidInput, match="already belongs"):
        await container.catalog.transfer_item(StoredKind.SKILL, "digest", None, "user")
    await container.catalog.transfer_item(StoredKind.MEMORY, "alpha", None, "user")
    assert (root(container) / "platform" / "memory" / "alpha.md").exists()
    await container.memory.put("trading", "x", actor="user")
    with pytest.raises(InvalidInput, match="named after the agent"):
        await container.catalog.transfer_item(StoredKind.MEMORY, "trading", alpha.id, "user")
    await container.catalog.transfer_item(StoredKind.MEMORY, "alpha", alpha.id, "user")
    assert (root(container) / "alpha" / "memory.md").exists()


# ---- agent 的建、改、删 ----


async def test_creating_and_renaming_an_agent_writes_its_directory_and_rules(container, make_agent):
    await container.distribution.create(RoleSpec("reader", "r", [], []), actor="user")
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
    # 私有记忆名就是 agent 名：与共享、全局记忆同名的 agent 建不了
    with pytest.raises(Conflict, match="memory named 'global'"):
        await container.agents.create(AgentSpec(name="global", description="d"), actor="user")

    # 手写的注释保留；改名挪目录，access.toml 里那一节跟着改名
    write(r / "alpha" / "agent.toml", "# keep me\n" + (r / "alpha" / "agent.toml").read_text())
    await container.assets.snapshot()
    updated = await container.agents.update(
        agent.id, AgentPatch(name="beta", description="new"), actor="user"
    )
    assert (updated.name, updated.description) == ("beta", "new")
    assert not (r / "alpha").exists()
    assert (r / "beta" / "agent.toml").read_text().startswith("# keep me\n")
    access = (r / "platform" / "access.toml").read_text()
    assert "[agents.beta]" in access and "[agents.alpha]" not in access
    assert (await container.distribution.agent_rules(agent.id)).roles == ("reader",)


async def test_deleting_an_agent_removes_its_directory_but_keeps_what_others_use(container, make_agent):
    alpha = await make_agent("alpha")
    await container.skills.write("kept", "When kept", "x", actor="user", owner=alpha.id)
    await container.skills.write("gone", "When gone", "x", actor="user", owner=alpha.id)
    await container.distribution.create(RoleSpec("readers", "r", ["use:skill:kept"], []), actor="user")
    await make_agent("beta", roles=("readers",))
    await container.agent_lifecycle.delete(alpha.id, actor="user")
    await drain(container)
    r = root(container)
    assert not (r / "alpha").exists()
    assert (r / "platform" / "skills" / "kept" / "SKILL.md").exists()
    assert not (r / "platform" / "skills" / "gone").exists()
    assert "[agents.alpha]" not in (r / "platform" / "access.toml").read_text()


# ---- 一次性导出 ----


async def test_the_database_is_exported_once_into_files(container, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    db = container.db
    async with db.transaction() as s:
        s.add(
            Agent(id="a1b2c3d4e5f6", name="alpha", description="d", model="test", thinking="low", group="g")
        )
        await s.flush()
        s.add(AgentRulesRow(agent_id="a1b2c3d4e5f6", roles=["reader"], allow=["write:path:~/x"], deny=[]))
        s.add(RoleRow(name="reader", description="r", allow=["use:skill:digest"], deny=[]))
        await s.merge(RoleRow(name="base", description="b", allow=["use:skill:digest"], deny=[]))
        s.add(
            Item(
                kind="memory",
                name="alpha",
                owner="a1b2c3d4e5f6",
                content="see ~/.muliao/data/skills/digest/x.py",
            )
        )
        s.add(Item(kind="memory", name="trading", owner=None, content="t"))
        md = (
            "---\nname: digest\ndescription: When digesting\n---\nRun $HOME/.muliao/data/skills/digest/x.py\n"
        )
        s.add(
            Item(kind="skill", name="digest", owner="a1b2c3d4e5f6", description="When digesting", content=md)
        )
        s.add(Item(kind="secret", name="TOKEN", owner=None, description="t", secret=b"gAAAAtoken"))
        s.add(
            Item(
                kind="mcp",
                name="docs",
                owner=None,
                enabled=False,
                content=json.dumps(
                    {
                        "transport": "http",
                        "command": None,
                        "args": [],
                        "url": "https://x",
                        "env": {},
                        "headers": {},
                    }
                ),
            )
        )
        await s.flush()
        digest = await s.scalar(select(Item.id).where(Item.name == "digest"))
        s.add(ItemFile(item_id=digest, path="x.py", content="print(1)\n", executable=True))
    target = tmp_path / ".muliao" / "assets"
    legacy = tmp_path / ".muliao" / "data" / "skills"
    (legacy / "digest").mkdir(parents=True)
    os.chmod(legacy / "digest", 0o555)
    assert await export_if_missing(db, target, legacy)
    assert not legacy.exists()
    assert not await export_if_missing(db, target, legacy)

    snapshot = load_tree(target, {"a1b2c3d4e5f6": "alpha"}, 4000)
    assert snapshot.problems == ()
    agent = snapshot.agents["a1b2c3d4e5f6"]
    assert (agent.name, agent.thinking, agent.group) == ("alpha", "low", "g")
    assert snapshot.rules_of(agent.id).roles == ("reader",)
    assert snapshot.rules_of(agent.id).allow == ("write:path:~/x",)
    assert snapshot.roles["base"].rules.allow == ("use:skill:digest",)
    digest_dir = target / "alpha" / "skills" / "digest"
    assert snapshot.asset(StoredKind.SKILL, "digest").path == digest_dir
    assert os.access(digest_dir / "x.py", os.X_OK)
    # 正文里写死的旧 skill 目录换成新位置，写法不变
    assert "Run $HOME/.muliao/assets/alpha/skills/digest/x.py" in (digest_dir / "SKILL.md").read_text()
    assert snapshot.asset(StoredKind.MEMORY, "alpha").text == "see ~/.muliao/assets/alpha/skills/digest/x.py"
    assert snapshot.asset(StoredKind.SECRET, "TOKEN").ciphertext == b"gAAAAtoken"
    assert snapshot.asset(StoredKind.MCP, "docs").mcp[1] is False
    assert snapshot.asset(StoredKind.MEMORY, "trading").owner is None
