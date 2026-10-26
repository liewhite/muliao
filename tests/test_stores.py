"""条目（skill、MCP server、secret）与记忆的存放：数据库里的表、版本、skill 的生成目录（#230）。"""

import asyncio
import os
import stat

import pytest

from muliao.agents.api import Subject
from muliao.agents.mcp import McpTransport, mcp_config
from muliao.agents.memory import MemoryFull
from muliao.agents.skill_package import SkillFile
from muliao.agents.skills import attachments_from, parse_document
from muliao.errors import Conflict, CorruptState, InvalidInput, NotFound
from muliao.vault.api import fill
from tests.test_flow import drain


async def _two(make_agent):
    return (await make_agent("alpha")).id, (await make_agent("beta")).id


# ---- skill ----


async def test_an_agent_sees_its_own_and_public_skills_but_not_other_agents(container, make_agent):
    skills = container.skills
    alpha, beta = await _two(make_agent)
    await skills.write(None, "digest", "public digest", "public body", "user")
    await skills.write(alpha, "mine", "alpha's", "alpha body", alpha)
    await skills.write(beta, "theirs", "beta's", "beta body", beta)

    assert [(s.name, s.scope) for s in await skills.index(alpha)] == [
        ("mine", "private"),
        ("digest", "public"),
    ]
    assert (await skills.resolve(alpha, "mine")).body.strip() == "alpha body"
    with pytest.raises(NotFound):
        await skills.resolve(alpha, "theirs")
    assert [s.name for s in await skills.list(None)] == ["digest"]


async def test_skill_names_are_unique_across_owners(container, make_agent):
    skills = container.skills
    alpha, beta = await _two(make_agent)
    await skills.write(None, "digest", "public", "x", "user")
    await skills.write(alpha, "mine", "alpha's", "x", alpha)

    with pytest.raises(Conflict, match="public skill named 'digest' already exists"):
        await skills.write(alpha, "digest", "shadow", "x", alpha)
    with pytest.raises(Conflict, match="private skill named 'mine'"):
        await skills.write(beta, "mine", "steal", "x", beta)
    with pytest.raises(Conflict):
        await skills.write(None, "mine", "steal", "x", "user")
    # 改不到别人的
    with pytest.raises(NotFound):
        await skills.edit(beta, "mine", beta, new="more")
    with pytest.raises(NotFound):
        await skills.delete(None, "mine", "user")


async def test_skill_roundtrip_and_validation(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    await skills.write(alpha, "slack-digest", "Summarise slack", "# Steps\n1. fetch", alpha)

    skill = await skills.read(alpha, "slack-digest")
    assert skill.meta.description == "Summarise slack"
    assert skill.body.startswith("# Steps")
    with pytest.raises(InvalidInput):
        await skills.write(alpha, "Bad Name", "d", "b", alpha)
    with pytest.raises(InvalidInput):
        await skills.write(alpha, "ok", "  ", "b", alpha)
    with pytest.raises(NotFound):
        await skills.delete(alpha, "missing", alpha)


async def test_editing_a_skill_changes_body_description_or_attachments_in_one_revision(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    await skills.write(alpha, "digest", "d1", "step one", alpha)

    await skills.edit(alpha, "digest", alpha, old="one", new="two", description="d2")
    skill = await skills.read(alpha, "digest")
    assert (skill.body.strip(), skill.meta.description) == ("step two", "d2")
    await skills.edit(alpha, "digest", alpha, attachments={"run.sh": SkillFile("echo hi\n", True)})
    with pytest.raises(InvalidInput, match="nothing to change"):
        await skills.edit(alpha, "digest", alpha)
    with pytest.raises(InvalidInput):
        await skills.edit(alpha, "digest", alpha, attachments={"../x.py": SkillFile("x")})

    [attach, edit, create] = await container.revisions.history(Subject.SKILL, "digest", 10)
    assert (create.before, edit.author, attach.message) == (None, alpha, "edit private skill digest")
    assert set(parse_document(attach.after)) == {"SKILL.md", "run.sh"}
    assert parse_document(attach.after)["SKILL.md"].content == parse_document(edit.after)["SKILL.md"].content


async def test_the_skill_directory_is_a_read_only_copy_generated_from_the_database(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    files = {"run.sh": SkillFile("#!/bin/sh\necho hi\n", True), "ref/notes.md": SkillFile("notes")}
    meta = await skills.write(alpha, "digest", "d", "body", alpha, attachments=files)

    directory = meta.path.parent
    assert directory == skills.skills_dir / "digest"
    assert (directory / "ref" / "notes.md").read_text() == "notes"
    assert os.access(directory / "run.sh", os.X_OK)
    assert not os.access(directory / "SKILL.md", os.W_OK)
    with pytest.raises(PermissionError):
        (directory / "new.py").write_text("x")

    # 只改正文：附件保留；删掉附件：目录跟着变
    await skills.edit(alpha, "digest", alpha, new="more")
    assert (directory / "run.sh").exists()
    await skills.edit(alpha, "digest", alpha, attachments={})
    assert sorted(p.name for p in directory.iterdir()) == ["SKILL.md"]
    await skills.delete(alpha, "digest", alpha)
    assert not directory.exists()


async def test_startup_regenerates_every_skill_directory_and_drops_strays(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    meta = await skills.write(alpha, "digest", "d", "body", alpha)
    directory = meta.path.parent
    # 被改动的副本、没有对应条目的目录、上次留下的临时目录
    directory.chmod(stat.S_IRWXU)
    (directory / "SKILL.md").chmod(stat.S_IRWXU)
    (directory / "SKILL.md").write_text("tampered")
    (skills.skills_dir / "stray").mkdir()
    (skills.skills_dir / ".digest.old.x").mkdir()

    await skills.materialize_all()

    assert "tampered" not in (directory / "SKILL.md").read_text()
    assert sorted(p.name for p in skills.skills_dir.iterdir()) == ["digest"]


async def test_attachments_are_taken_from_a_directory_by_the_attachment_rules(tmp_path):
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "lib" / "a.py").write_text("print(1)")
    (src / "SKILL.md").write_text("ignored: given separately")
    (src / ".venv").mkdir()
    (src / ".venv" / "x.py").write_text("hidden")
    (src / "blob.bin").write_bytes(b"\0\1")
    (src / "big.txt").write_text("x" * (300 * 1024))
    (src / "link.py").symlink_to(src / "lib" / "a.py")

    files, skipped = attachments_from(src)

    assert set(files) == {"lib/a.py"}
    assert {s.path for s in skipped} >= {"blob.bin", "big.txt", "link.py"}
    with pytest.raises(InvalidInput):
        attachments_from(tmp_path / "missing")


async def test_a_skill_is_restored_from_an_earlier_revision(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    await skills.write(alpha, "digest", "d", "first", alpha, attachments={"a.py": SkillFile("1")})
    await skills.write(alpha, "digest", "d", "second", alpha, attachments={})
    [_, first] = await container.revisions.history(Subject.SKILL, "digest", 10)

    await skills.restore("digest", first.after, "user")

    skill = await skills.read(alpha, "digest")
    assert skill.body.strip() == "first"
    assert (skills.skills_dir / "digest" / "a.py").read_text() == "1"


# ---- 记忆 ----


async def test_memory_append_replace_limit_and_revisions(container, make_agent):
    memory = container.memory
    alpha = (await make_agent("alpha")).id
    await memory.edit(alpha, "", "- likes tea", alpha)
    await memory.edit(alpha, "", "- lives in Paris", alpha)
    await memory.edit(alpha, "Paris", "Berlin", alpha)
    assert await memory.read(alpha) == "- likes tea\n\n- lives in Berlin\n"

    with pytest.raises(InvalidInput, match="found 2"):
        await memory.edit(alpha, "-", "+", alpha)
    with pytest.raises(MemoryFull):
        await memory.edit(alpha, "", "x" * 5000, alpha)
    revisions = await container.revisions.history(Subject.MEMORY, alpha, 10)
    assert [r.before for r in revisions][-1] is None
    assert len(revisions) == 4


async def test_a_missing_memory_row_is_corrupt_state_and_global_memory_starts_empty(container):
    assert await container.memory.read(None) == ""
    with pytest.raises(CorruptState):
        await container.memory.read("c3c3c3c3c3c3")


async def test_concurrent_memory_edits_are_all_kept(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await asyncio.gather(*(container.memory.edit(alpha, "", f"- fact {i}", alpha) for i in range(5)))
    assert sorted(line for line in (await container.memory.read(alpha)).split("\n") if line) == [
        f"- fact {i}" for i in range(5)
    ]


# ---- MCP ----


async def test_mcp_servers_have_unique_names_and_versions(container, make_agent):
    mcp = container.mcp
    alpha = (await make_agent("alpha")).id
    config = mcp_config(transport=McpTransport.HTTP, url="https://example.com/mcp")
    await mcp.add(None, "search", config, probe=False, author="user")
    await mcp.add(alpha, "own", config, probe=False, author=alpha)

    with pytest.raises(Conflict):
        await mcp.add(alpha, "search", config, probe=False, author=alpha)
    assert [(s.name, active) for s, active in await mcp.listing(alpha, ["search"])] == [
        ("own", True),
        ("search", True),
    ]
    assert [(s.name, active) for s, active in await mcp.listing(alpha, [])] == [
        ("own", True),
        ("search", False),
    ]

    await mcp.set_enabled(alpha, "own", False, alpha)
    assert [s.name for s in await mcp.index(alpha, ["search"])] == ["search"]
    [disabled, added] = await container.revisions.history(Subject.MCP, "own", 10)
    await mcp.restore("own", added.after, "user")
    assert (await mcp.get(alpha, "own")).enabled
    assert disabled.message == "disable private MCP server own"

    await mcp.remove(alpha, "own", alpha)
    with pytest.raises(NotFound):
        await mcp.get(alpha, "own")


# ---- secret ----


async def test_vault_renders_references_and_fails_on_missing_key(container):
    vault = container.vault
    await vault.set(None, "TOKEN", "s3cret", "api token", "user")

    assert fill("Bearer ${vault:TOKEN}", await vault.resolve_many(None, ["TOKEN"])) == "Bearer s3cret"
    assert [s.key for s in await vault.visible_to(None)] == ["TOKEN"]
    with pytest.raises(NotFound):
        await vault.resolve_many(None, ["MISSING"])
    with pytest.raises(InvalidInput):
        await vault.set(None, "bad-key", "v", "", "user")


async def test_secrets_are_visible_to_their_owner_and_keys_are_unique(container, make_agent):
    vault = container.vault
    alpha, beta = await _two(make_agent)
    await vault.set(None, "TOKEN", "public", "", "user")
    await vault.set(alpha, "ALPHA_KEY", "a", "", alpha)

    with pytest.raises(Conflict):
        await vault.set(alpha, "TOKEN", "alpha-only", "", alpha)
    assert await vault.resolve_many(alpha, ["TOKEN", "ALPHA_KEY"]) == {"TOKEN": "public", "ALPHA_KEY": "a"}
    with pytest.raises(NotFound):
        await vault.resolve_many(beta, ["ALPHA_KEY"])
    assert [(s.key, s.owner) for s in await vault.visible_to(alpha)] == [
        ("ALPHA_KEY", alpha),
        ("TOKEN", None),
    ]

    # 改值也记一版，版本里没有值
    await vault.set(alpha, "ALPHA_KEY", "b", "", alpha)
    revisions = await container.revisions.history(Subject.SECRET, "ALPHA_KEY", 10)
    assert len(revisions) == 2
    assert all(
        "b" not in (r.after or "").replace("description", "").replace("value_set_at", "") for r in revisions
    )

    await vault.delete(alpha, "ALPHA_KEY", alpha)
    with pytest.raises(NotFound):
        await vault.delete(alpha, "ALPHA_KEY", alpha)


async def test_an_agents_items_and_memory_go_with_it_and_their_deletion_is_recorded(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await container.vault.set(alpha, "TOKEN", "x", "", alpha)
    meta = await container.skills.write(alpha, "digest", "d", "b", alpha)
    await container.mcp.add(
        alpha, "own", mcp_config(transport=McpTransport.HTTP, url="https://e.com"), probe=False, author=alpha
    )
    await container.memory.edit(None, "", "- global fact", alpha)

    await container.agent_lifecycle.delete(alpha, author="user")
    # 删除的清理经事件完成
    await drain(container)

    assert await container.vault.visible_to(alpha) == []
    assert await container.skills.list(alpha) == []
    assert not meta.path.parent.exists()
    for subject, name in [
        (Subject.SECRET, "TOKEN"),
        (Subject.SKILL, "digest"),
        (Subject.MCP, "own"),
        (Subject.MEMORY, alpha),
    ]:
        latest = (await container.revisions.history(subject, name, 1))[0]
        assert (latest.after, latest.author, latest.message) == (None, "user", f"deleted with agent {alpha}")
    # 它写过的全局记忆留着
    assert await container.memory.read(None) == "- global fact\n"


async def test_writes_for_a_deleting_agent_are_refused(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await container.agent_lifecycle.delete(alpha, author="user")

    with pytest.raises(NotFound):
        await container.skills.write(alpha, "late", "d", "b", alpha)
    with pytest.raises(NotFound):
        await container.memory.edit(alpha, "", "late", alpha)
    with pytest.raises(NotFound):
        await container.vault.set(alpha, "LATE", "x", "", alpha)


@pytest.mark.parametrize("meddle", ["rewrite", "delete"])
async def test_an_edit_whose_skill_changed_or_vanished_after_it_was_read_is_a_conflict(
    container, make_agent, monkeypatch, meddle
):
    """edit 先读后写：读到之后、写入之前别人改了或删了这个 skill，写入报冲突，不悄悄盖掉。"""
    store = container.skills
    alpha = (await make_agent("alpha")).id
    await store.write(alpha, "digest", "d", "body", alpha)
    put = store._put  # noqa: SLF001

    async def meddle_then_put(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        monkeypatch.setattr(store, "_put", put)
        if meddle == "rewrite":
            await store.write(alpha, "digest", "d", "rewritten", alpha)
        else:
            await store.delete(alpha, "digest", alpha)
        return await put(*args, **kwargs)

    monkeypatch.setattr(store, "_put", meddle_then_put)
    with pytest.raises(Conflict, match="changed at the same time"):
        await store.edit(alpha, "digest", alpha, new="more")
    if meddle == "rewrite":
        assert (await store.read(alpha, "digest")).body.strip() == "rewritten"
    else:
        assert await store.list(alpha) == []


async def test_hidden_and_dependency_directories_are_skipped_whole(tmp_path):
    src = tmp_path / "src"
    (src / "node_modules" / "pkg").mkdir(parents=True)
    for i in range(50):
        (src / "node_modules" / "pkg" / f"{i}.js").write_text("x")
    (src / "run.py").write_text("x")

    files, skipped = attachments_from(src)

    assert set(files) == {"run.py"}
    assert [s.path for s in skipped] == ["node_modules/"]


async def test_memory_edit_with_nothing_to_append_is_rejected_and_unchanged(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await container.memory.edit(alpha, "", "- kept", alpha)
    with pytest.raises(InvalidInput, match="new_text is empty"):
        await container.memory.edit(alpha, "", "  ", alpha)
    assert await container.memory.read(alpha) == "- kept\n"
