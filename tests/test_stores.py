"""条目（skill、MCP server、secret）与记忆的存放：数据库里的表、版本、skill 的生成目录（#230）。"""

import asyncio
import os
import stat

import pytest

from muliao.agents.api import Subject, fill
from muliao.agents.mcp import McpTransport, mcp_config
from muliao.agents.memory import MemoryFull
from muliao.agents.skill_package import SKILL_FILE, SkillFile
from muliao.agents.skills import attachments_from, parse_document
from muliao.errors import Conflict, CorruptState, Forbidden, InvalidInput, NotFound
from tests.conftest import history, owned, share
from tests.test_flow import drain


async def _two(make_agent):
    return (await make_agent("alpha")).id, (await make_agent("beta")).id


# ---- skill ----


async def test_an_agent_sees_its_own_skills_and_those_its_rules_give_it(container, make_agent):
    skills = container.skills
    alpha, beta = await _two(make_agent)
    await skills.write("digest", "public digest", "public body", actor="user", owner=None)
    await skills.write("mine", "alpha's", "alpha body", actor=alpha, owner=alpha)
    await skills.write("theirs", "beta's", "beta body", actor=beta, owner=beta)

    # 归平台的不自动给谁：进了 base 才人人可用
    assert [(s.name, s.owner) for s in await skills.index(alpha)] == [("mine", alpha)]
    await share(container, "skill:digest")
    assert [s.name for s in await skills.index(alpha)] == ["digest", "mine"]
    assert (await skills.resolve(alpha, "mine")).body.strip() == "alpha body"
    assert not (await skills.resolve(alpha, "theirs")).usable
    with pytest.raises(NotFound):
        await skills.resolve(alpha, "nope")
    assert await owned(container, "skill", None) == ["digest"]


async def test_skill_names_are_unique_across_owners(container, make_agent):
    skills = container.skills
    alpha, beta = await _two(make_agent)
    await skills.write("digest", "public", "x", actor="user", owner=None)
    await skills.write("mine", "alpha's", "x", actor=alpha, owner=alpha)

    # 同名就是同一个条目：不是 owner 也不是 admin 的改不了（没有同名遮蔽）
    with pytest.raises(Forbidden, match="skill 'digest' belongs to the platform"):
        await skills.write("digest", "shadow", "x", actor=alpha, owner=alpha)
    with pytest.raises(Forbidden, match="skill 'mine' belongs to agent 'alpha'"):
        await skills.write("mine", "steal", "x", actor=beta, owner=beta)
    with pytest.raises(Forbidden):
        await skills.edit("mine", beta, new="more")
    # 用户能改任何条目，owner 不变
    meta = await skills.write("mine", "by user", "x", actor="user", owner=None)
    assert meta.owner == alpha


async def test_skill_roundtrip_and_validation(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    await skills.write("slack-digest", "Summarise slack", "# Steps\n1. fetch", actor=alpha, owner=alpha)

    skill = await skills.read("slack-digest")
    assert skill.meta.description == "Summarise slack"
    assert skill.body.startswith("# Steps")
    with pytest.raises(InvalidInput):
        await skills.write("Bad Name", "d", "b", actor=alpha, owner=alpha)
    with pytest.raises(InvalidInput):
        await skills.write("ok", "  ", "b", actor=alpha, owner=alpha)
    with pytest.raises(NotFound):
        await skills.delete("missing", alpha)


async def test_editing_a_skill_changes_body_description_or_attachments_in_one_revision(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    await skills.write("digest", "d1", "step one", actor=alpha, owner=alpha)

    await skills.edit("digest", alpha, old="one", new="two", description="d2")
    skill = await skills.read("digest")
    assert (skill.body.strip(), skill.meta.description) == ("step two", "d2")
    await skills.edit("digest", alpha, attachments={"run.sh": SkillFile("echo hi\n", True)})
    with pytest.raises(InvalidInput, match="nothing to change"):
        await skills.edit("digest", alpha)
    with pytest.raises(InvalidInput):
        await skills.edit("digest", alpha, attachments={"../x.py": SkillFile("x")})

    [attach, edit, create] = await history(container, Subject.SKILL, "digest", 10)
    assert (create.before, edit.author, attach.message) == (None, alpha, "edit skill digest")
    assert set(parse_document(attach.after)) == {"SKILL.md", "run.sh"}
    assert parse_document(attach.after)["SKILL.md"].content == parse_document(edit.after)["SKILL.md"].content


async def test_the_skill_directory_is_a_read_only_copy_generated_from_the_database(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    files = {"run.sh": SkillFile("#!/bin/sh\necho hi\n", True), "ref/notes.md": SkillFile("notes")}
    meta = await skills.write("digest", "d", "body", actor=alpha, owner=alpha, attachments=files)

    directory = meta.path.parent
    assert directory == skills.skills_dir / "digest"
    assert (directory / "ref" / "notes.md").read_text() == "notes"
    assert os.access(directory / "run.sh", os.X_OK)
    assert not os.access(directory / "SKILL.md", os.W_OK)
    with pytest.raises(PermissionError):
        (directory / "new.py").write_text("x")

    # 只改正文：附件保留；删掉附件：目录跟着变
    await skills.edit("digest", alpha, new="more")
    assert (directory / "run.sh").exists()
    await skills.edit("digest", alpha, attachments={})
    assert sorted(p.name for p in directory.iterdir()) == ["SKILL.md"]
    await skills.delete("digest", alpha)
    assert not directory.exists()


async def test_startup_regenerates_every_skill_directory_and_drops_strays(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    meta = await skills.write("digest", "d", "body", actor=alpha, owner=alpha)
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
    await skills.write("digest", "d", "first", actor=alpha, owner=alpha, attachments={"a.py": SkillFile("1")})
    await skills.write("digest", "d", "second", actor=alpha, owner=alpha, attachments={})
    [_, first] = await history(container, Subject.SKILL, "digest", 10)

    await skills.restore("digest", first.id, "user")

    skill = await skills.read("digest")
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
    revisions = await history(container, Subject.MEMORY, alpha, 10)
    assert [r.before for r in revisions][-1] is None
    assert len(revisions) == 4


async def test_a_missing_memory_row_is_corrupt_state_and_global_memory_starts_empty(container, make_agent):
    from sqlalchemy import delete

    from muliao.agents.tables import Memory

    assert await container.memory.read(None) == ""
    with pytest.raises(NotFound):
        await container.memory.read("c3c3c3c3c3c3")
    alpha = (await make_agent()).id
    async with container.db.transaction() as s:
        await s.execute(delete(Memory).where(Memory.owner == alpha))
    with pytest.raises(CorruptState):
        await container.memory.read(alpha)


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
    await mcp.add("search", config, actor="user", owner=None, probe=False)
    await mcp.add("own", config, actor=alpha, owner=alpha, probe=False)

    with pytest.raises(Conflict):
        await mcp.add("search", config, actor=alpha, owner=alpha, probe=False)
    assert [s.name for s in await mcp.usable(alpha)] == ["own"]
    await share(container, "mcp:search")
    assert [s.name for s in await mcp.usable(alpha)] == ["own", "search"]

    await mcp.set_enabled("own", False, alpha)
    assert [s.name for s in await mcp.index(alpha)] == ["search"]
    assert [s.name for s in await mcp.usable(alpha)] == ["own", "search"]
    [disabled, added] = await history(container, Subject.MCP, "own", 10)
    await mcp.restore("own", added.id, "user")
    assert (await mcp.get("own")).enabled
    assert disabled.message == "disable MCP server own"

    await mcp.remove("own", alpha)
    with pytest.raises(NotFound):
        await mcp.get("own")


# ---- secret ----


async def test_vault_renders_references_and_fails_on_missing_key(container):
    vault = container.vault
    await vault.set("TOKEN", "s3cret", "api token", actor="user", owner=None)

    assert fill("Bearer ${vault:TOKEN}", await vault.resolve_many("user", ["TOKEN"])) == "Bearer s3cret"
    assert await owned(container, "secret", None) == ["TOKEN"]
    with pytest.raises(NotFound):
        await vault.resolve_many("user", ["MISSING"])
    with pytest.raises(InvalidInput):
        await vault.set("bad-key", "v", "", actor="user", owner=None)


async def test_secrets_are_visible_to_their_owner_and_keys_are_unique(container, make_agent):
    vault = container.vault
    alpha, beta = await _two(make_agent)
    await vault.set("TOKEN", "public", "", actor="user", owner=None)
    await vault.set("ALPHA_KEY", "a", "", actor=alpha, owner=alpha)

    with pytest.raises(Forbidden):
        await vault.set("TOKEN", "alpha-only", "", actor=alpha, owner=alpha)
    await share(container, "secret:TOKEN")
    assert await vault.resolve_many(alpha, ["TOKEN", "ALPHA_KEY"]) == {"TOKEN": "public", "ALPHA_KEY": "a"}
    with pytest.raises(NotFound):
        await vault.resolve_many(beta, ["ALPHA_KEY"])
    assert [(s.key, s.owner) for s in await vault.usable_by(alpha)] == [
        ("ALPHA_KEY", alpha),
        ("TOKEN", None),
    ]

    # 改值也记一版，版本里没有值
    await vault.set("ALPHA_KEY", "b", "", actor=alpha, owner=alpha)
    revisions = await history(container, Subject.SECRET, "ALPHA_KEY", 10)
    assert len(revisions) == 2
    assert all("b" not in (r.after or "").replace("description", "") for r in revisions)

    await vault.delete("ALPHA_KEY", alpha)
    with pytest.raises(NotFound):
        await vault.delete("ALPHA_KEY", alpha)


async def test_an_agents_items_and_memory_go_with_it_and_their_deletion_is_recorded(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await container.vault.set("TOKEN", "x", "", actor=alpha, owner=alpha)
    meta = await container.skills.write("digest", "d", "b", actor=alpha, owner=alpha)
    await container.mcp.add(
        "own",
        mcp_config(transport=McpTransport.HTTP, url="https://e.com"),
        actor=alpha,
        owner=alpha,
        probe=False,
    )
    await container.memory.edit(None, "", "- global fact", alpha)

    await container.agent_lifecycle.delete(alpha, author="user")
    # 删除的清理经事件完成
    await drain(container)

    assert await owned(container, "secret", alpha) == []
    assert await owned(container, "skill", alpha) == []
    assert not meta.path.parent.exists()
    for subject, name in [
        (Subject.SECRET, "TOKEN"),
        (Subject.SKILL, "digest"),
        (Subject.MCP, "own"),
        (Subject.MEMORY, alpha),
    ]:
        latest = (await history(container, subject, name, 1))[0]
        assert (latest.after, latest.author, latest.message) == (None, "user", f"deleted with agent {alpha}")
    # 它写过的全局记忆留着
    assert await container.memory.read(None) == "- global fact\n"


async def test_writes_for_a_deleting_agent_are_refused(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await container.agent_lifecycle.delete(alpha, author="user")

    with pytest.raises(NotFound):
        await container.skills.write("late", "d", "b", actor=alpha, owner=alpha)
    with pytest.raises(NotFound):
        await container.memory.edit(alpha, "", "late", alpha)
    with pytest.raises(NotFound):
        await container.vault.set("LATE", "x", "", actor=alpha, owner=alpha)


async def test_concurrent_edits_of_one_skill_all_apply(container, make_agent):
    """同一 skill 并发改不同段落：读改写在一个写事务里串行，全部成功、各记一版，不报冲突（#236）。"""
    store = container.skills
    alpha = (await make_agent("alpha")).id
    await store.write("digest", "d", "\n".join(f"line{i}" for i in range(6)), actor=alpha, owner=alpha)

    await asyncio.gather(*(store.edit("digest", alpha, old=f"line{i}", new=f"LINE{i}") for i in range(6)))

    assert (await store.read("digest")).body.split() == [f"LINE{i}" for i in range(6)]
    assert (store.dir("digest") / SKILL_FILE).read_text().count("LINE") == 6
    revs = await history(container, Subject.SKILL, "digest", 20)
    assert len(revs) == 7


async def test_an_edit_of_a_vanished_skill_is_not_found(container, make_agent):
    store = container.skills
    alpha = (await make_agent("alpha")).id
    await store.write("digest", "d", "body", actor=alpha, owner=alpha)
    await store.delete("digest", alpha)
    with pytest.raises(NotFound):
        await store.edit("digest", alpha, new="more")


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
