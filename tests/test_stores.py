import asyncio
import subprocess
from functools import partial
from pathlib import Path

import anyio
import pytest

from muliao.agents.memory import MemoryFull, MemoryStore
from muliao.agents.resources import MAX_EXTERNAL_FILE_BYTES, RESOURCE_PATHSPEC, SYSTEM, ResourceRepo
from muliao.agents.skills import SkillStore
from muliao.errors import CorruptState, InvalidInput, NotFound
from tests.test_flow import drain

# agent id 的格式（agents.ids.AGENT_ID）
ALPHA, BETA = "a1a1a1a1a1a1", "b2b2b2b2b2b2"


async def any_owner(_owner: str | None) -> None:
    """这些用例只测 store 本身：owner 是否还可用不在这里查（见 test_deletion 的资源写入用例）。"""


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, seed_resources) -> ResourceRepo:
    return ResourceRepo(seed_resources(tmp_path / "resources"), any_owner)


async def test_ensure_initialises_a_fresh_repository(tmp_path: Path):
    """其余用例用的是 conftest 里初始化过一次的模板仓库的副本，初始化本身在这里测。"""
    root = tmp_path / "fresh"
    await ResourceRepo(root, any_owner).ensure()

    assert _git("-C", str(root), "rev-list", "--count", "HEAD").strip() == "1"
    assert _git("-C", str(root), "config", "user.name").strip() == SYSTEM


@pytest.fixture
def skills(repo: ResourceRepo) -> SkillStore:
    return SkillStore(repo)


async def test_private_skill_shadows_public_skill_with_same_name(skills: SkillStore):
    await skills.write(None, "digest", "public digest", "public body", "user")
    await skills.write(ALPHA, "digest", "private digest", "private body", "user")
    await skills.write(None, "other", "other public", "x", "user")

    visible = {(s.name, s.scope) for s in skills.index(ALPHA)[0]}

    assert visible == {("digest", "private"), ("other", "public")}
    assert skills.resolve(ALPHA, "digest").body.strip() == "private body"
    assert skills.resolve(BETA, "digest").body.strip() == "public body"


async def test_skill_roundtrip_preserves_frontmatter(skills: SkillStore):
    await skills.write(ALPHA, "slack-digest", "Summarise slack", "# Steps\n1. fetch", "user")

    skill = skills.read(ALPHA, "slack-digest")

    assert skill.meta.description == "Summarise slack"
    assert skill.body.startswith("# Steps")


async def test_skill_rejects_invalid_name_and_empty_description(skills: SkillStore):
    with pytest.raises(InvalidInput):
        await skills.write(None, "../escape", "d", "b", "user")
    with pytest.raises(InvalidInput):
        await skills.write(None, "ok", "  ", "b", "user")


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("---\nname: fake\ndescription: x\n---\nbody", "does not match directory"),
        ("---\nname: [real\n---\nbody", "cannot parse"),
        ("no frontmatter", "must contain string 'name'"),
    ],
)
async def test_a_corrupt_skill_is_reported_on_its_own_without_hiding_the_others(
    skills: SkillStore, repo: ResourceRepo, content: str, problem: str
):
    await skills.write(None, "good", "fine", "x", "user")
    await skills.write(ALPHA, "mine", "fine too", "x", "user")
    d = repo.owner_dir(None) / "skills" / "real"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content)

    assert [s.name for s in skills.index(ALPHA)[0]] == ["mine", "good"]
    [corrupt] = skills.index(ALPHA)[1]
    assert (corrupt.name, corrupt.owner, corrupt.kind) == ("real", None, "skill")
    assert problem in corrupt.problem and str(d) in corrupt.problem
    assert "public skill 'real' is corrupt" in corrupt.describe()
    # 读它本身照样报错（交还模型的那种），读出原因；删得掉
    with pytest.raises(CorruptState, match=problem):
        skills.read(None, "real")
    with pytest.raises(NotFound, match=problem):
        skills.resolve(ALPHA, "real")
    # 局部修改交还调用方（模型照索引说明来修时不让运行失败），整篇重写即修复
    with pytest.raises(InvalidInput, match="rewrite it as a whole"):
        await skills.edit(None, "real", "user", description="new")
    assert skills.corrupt_in(None) == [corrupt]
    await skills.write(None, "real", "fixed", "x", "user")
    assert skills.index(ALPHA)[1] == []
    await skills.delete(None, "real", "user")


async def test_corrupt_skills_of_other_agents_are_not_reported(skills: SkillStore, repo: ResourceRepo):
    d = repo.owner_dir(BETA) / "skills" / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("broken")

    assert skills.index(ALPHA)[1] == []
    assert [c.name for c in skills.index(BETA)[1]] == ["broken"]


async def test_corrupt_items_are_logged_once_where_handed_out_and_dangling_links_are_skipped(
    skills: SkillStore, repo: ResourceRepo, caplog
):
    """坏项只在交出它的地方记一次 error；悬空的符号链接（agent 用 shell 留下的）不算一项，也不让列出失败。"""
    await skills.write(None, "good", "fine", "x", "user")
    d = repo.owner_dir(None) / "skills" / "broken"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("no frontmatter")
    mcp_dir = repo.owner_dir(None) / "mcp"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "bad.json").write_text("{not json")
    (repo.owner_dir(None) / "skills" / "dangling").mkdir()
    (repo.owner_dir(None) / "skills" / "dangling" / "SKILL.md").symlink_to(d / "missing.md")
    (mcp_dir / "gone.json").symlink_to(mcp_dir / "missing.json")
    from muliao.agents.mcp import McpRegistry

    async def no_probe(_server):
        raise AssertionError("not probed")

    mcp = McpRegistry(repo, no_probe)

    def logged() -> list[str]:
        return [r.resource for r in caplog.records if r.getMessage() == "corrupt resource"]

    # 管理界面：列出读得出来的不记，交出坏的那次记
    assert [s.name for s in skills.list(None)] == ["good"]
    assert logged() == []
    assert [c.name for c in skills.corrupt_in(None)] == ["broken"]
    assert mcp.list(None) == [] and [c.name for c in mcp.corrupt_in(None)] == ["bad"]
    assert logged() == ["broken", "bad"]
    caplog.clear()
    visible, corrupt = skills.index(ALPHA)
    assert [s.name for s in visible] == ["good"] and [c.name for c in corrupt] == ["broken"]
    assert logged() == ["broken"]


async def test_delete_missing_skill_raises_not_found(skills: SkillStore):
    with pytest.raises(NotFound):
        await skills.delete(None, "nope", "user")


async def test_every_change_is_a_revision_with_its_author(skills: SkillStore, repo: ResourceRepo):
    await skills.write(ALPHA, "digest", "v1", "one", "user")
    await skills.write(ALPHA, "digest", "v2", "two", ALPHA)
    await skills.write(ALPHA, "digest", "v2", "two", ALPHA)  # 没有变化：不新增版本
    path = repo.owner_dir(ALPHA) / "skills" / "digest" / "SKILL.md"

    history = await repo.history(path, 10)

    assert [r.author for r in history] == [ALPHA, "user"]
    assert "one" in await repo.read_at(history[1].rev, path)
    with pytest.raises(InvalidInput):
        await repo.read_at("--output=/tmp/x", path)


@pytest.fixture
async def memory(repo: ResourceRepo) -> MemoryStore:
    store = MemoryStore(repo, max_chars=50)
    await repo.apply({store.path(ALPHA): ""}, "init", "user")
    return store


async def test_memory_append_and_replace(memory: MemoryStore):
    await memory.append(ALPHA, "- likes tea", ALPHA)
    await memory.append(ALPHA, "- lives in Paris", ALPHA)
    await memory.replace(ALPHA, "Paris", "Berlin", ALPHA)

    assert memory.read(ALPHA) == "- likes tea\n\n- lives in Berlin\n"


async def test_memory_replace_requires_exactly_one_occurrence(memory: MemoryStore):
    await memory.append(ALPHA, "a a", ALPHA)
    with pytest.raises(InvalidInput, match="found 2"):
        await memory.replace(ALPHA, "a", "b", ALPHA)


async def test_memory_over_limit_is_rejected_and_unchanged(memory: MemoryStore):
    await memory.append(ALPHA, "short", ALPHA)
    with pytest.raises(MemoryFull):
        await memory.append(ALPHA, "x" * 60, ALPHA)
    assert memory.read(ALPHA) == "short\n"


async def test_a_missing_memory_file_is_corrupt_state_not_a_caller_error(memory: MemoryStore):
    with pytest.raises(CorruptState):
        memory.read("c3c3c3c3c3c3")


async def test_vault_renders_references_and_fails_on_missing_key(container):
    vault = container.vault
    await vault.set(None, "TOKEN", "s3cret", "api token")

    from muliao.vault.api import fill

    assert fill("Bearer ${vault:TOKEN}", await vault.resolve_many(None, ["TOKEN"])) == "Bearer s3cret"
    assert [(s.key, shadowed) for s, shadowed in await vault.view(None)] == [("TOKEN", False)]
    with pytest.raises(NotFound):
        await vault.resolve_many(None, ["MISSING"])
    with pytest.raises(InvalidInput):
        await vault.set(None, "bad-key", "v")


async def test_private_secret_shadows_public_and_is_invisible_to_others(container, make_agent):
    vault = container.vault
    alpha, beta = (await make_agent(ALPHA)).id, (await make_agent(BETA)).id
    await vault.set(None, "TOKEN", "public")
    await vault.set(alpha, "TOKEN", "alpha-only")
    await vault.set(alpha, "ALPHA_KEY", "a")

    assert await vault.resolve_many(alpha, ["TOKEN"]) == {"TOKEN": "alpha-only"}
    assert await vault.resolve_many(beta, ["TOKEN"]) == {"TOKEN": "public"}
    # 公共视角（公共 MCP server 的配置）只看公共 secret
    assert await vault.resolve_many(None, ["TOKEN"]) == {"TOKEN": "public"}
    with pytest.raises(NotFound):
        await vault.resolve_many(beta, ["ALPHA_KEY"])
    assert [(s.key, s.owner) for s in await vault.visible_to(alpha)] == [
        ("ALPHA_KEY", alpha),
        ("TOKEN", alpha),
    ]

    # 管理界面的视角：私有在前，公共的标明被遮蔽；别的 agent 视角里不遮蔽
    assert [(s.key, s.owner, shadowed) for s, shadowed in await vault.view(alpha)] == [
        ("ALPHA_KEY", alpha, False),
        ("TOKEN", alpha, False),
        ("TOKEN", None, True),
    ]
    assert [(s.key, shadowed) for s, shadowed in await vault.view(beta)] == [("TOKEN", False)]

    await vault.delete(alpha, "TOKEN")
    assert await vault.resolve_many(alpha, ["TOKEN"]) == {"TOKEN": "public"}
    with pytest.raises(NotFound):
        await vault.delete(alpha, "TOKEN")


async def test_secrets_of_deleted_agent_go_with_it(container, make_agent):
    alpha = (await make_agent(ALPHA)).id
    await container.vault.set(alpha, "TOKEN", "x")
    await container.agent_lifecycle.delete(alpha, author="user")
    # 删除的清理经事件完成
    await drain(container)
    assert await container.vault.view(alpha) == []


async def test_recover_commits_leftovers_and_clears_a_stale_lock(repo: ResourceRepo):
    """进程在写完文件、提交之前被杀，或在 git 运行中被杀：启动时收敛到干净状态。"""
    await repo.ensure()
    left = repo.owner_dir(ALPHA) / "MEMORY.md"
    left.parent.mkdir(parents=True)
    left.write_text("written but never committed")
    (repo.root / ".git" / "index.lock").write_text("")

    await repo.recover()

    assert not (repo.root / ".git" / "index.lock").exists()
    [revision] = await repo.history(left, 10)
    assert (revision.author, revision.message) == (
        "muliao",
        "reconcile changes made outside the resource tools",
    )
    # 收敛后正常提交照常进行
    assert await repo.apply({left: "next"}, "memory", ALPHA) is not None


async def test_deleting_an_agent_is_recorded_in_resource_history(container, make_agent):
    agent = await make_agent(ALPHA)
    memory = container.memory.path(agent.id)

    await container.agent_lifecycle.delete(agent.id, author="user")
    # 删除的清理经事件完成
    await drain(container)

    [deleted, created] = await container.resources.history(memory, 10)
    assert (deleted.author, deleted.message) == ("user", f"delete agent {agent.id}")
    assert created.message == f"create agent {agent.id}"


async def test_concurrent_edits_of_one_file_are_all_kept(memory: MemoryStore):
    import asyncio

    await asyncio.gather(*(memory.append(ALPHA, f"- fact {i}", ALPHA) for i in range(5)))

    assert sorted(memory.read(ALPHA).split()) == sorted(
        ["-", "fact", "0", "-", "fact", "1", "-", "fact", "2", "-", "fact", "3", "-", "fact", "4"]
    )


async def test_failed_commit_leaves_nothing_behind_and_a_retry_works(
    memory: MemoryStore, repo: ResourceRepo, monkeypatch
):
    await memory.append(ALPHA, "- kept", ALPHA)
    real_git = repo._git

    async def failing_commit(*args, check=True):  # noqa: ANN001, ANN202
        if args and args[0] == "commit":
            raise RuntimeError("disk full")
        return await real_git(*args, check=check)

    monkeypatch.setattr(repo, "_git", failing_commit)
    with pytest.raises(RuntimeError, match="disk full"):
        await memory.append(ALPHA, "- lost", ALPHA)
    monkeypatch.undo()

    # 报错即没有生效：文件回到上一次提交，重试不会重复
    assert memory.read(ALPHA) == "- kept\n"
    await memory.append(ALPHA, "- lost", ALPHA)
    assert memory.read(ALPHA) == "- kept\n\n- lost\n"


def test_owner_ids_cannot_escape_the_repository(repo: ResourceRepo):
    for bad in ("..", "../public", "../../etc", "ALPHA"):
        with pytest.raises(InvalidInput, match="invalid agent id"):
            repo.owner_dir(bad)


async def test_corrupt_mcp_config_is_reported_with_its_path_on_its_own_and_can_be_deleted(container):
    from muliao.agents.api import McpTransport, mcp_config

    spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
    await container.mcp.add(None, "echo", spec, probe=False, author="user")
    path = container.resources.owner_dir(None) / "mcp" / "echo.json"
    import json

    original = path.read_text()
    # 坏的 JSON、类型不对、缺字段（被停用的 server 丢了 enabled 不能悄悄变回启用）
    missing = json.dumps({k: v for k, v in json.loads(original).items() if k != "enabled"})
    for broken in ('{"not json"', original.replace("true", '"false"'), missing):
        path.write_text(broken)
        with pytest.raises(CorruptState, match=f"corrupt MCP config {path}"):
            container.mcp.get(None, "echo")
        # 列出时单独报出，不连累别的 server
        assert container.mcp.list(None) == []
        [corrupt] = container.mcp.index("a1a1a1a1a1a1")[1]
        assert container.mcp.corrupt_in(None) == [corrupt]
        with pytest.raises(InvalidInput, match="remove it and add it again"):
            await container.mcp.set_enabled(None, "echo", False, "user")
        assert (corrupt.kind, corrupt.name, corrupt.path) == ("MCP server", "echo", path)
        assert f"corrupt MCP config {path}" in corrupt.problem

    await container.mcp.remove(None, "echo", "user")
    assert container.mcp.list(None) == []


async def test_write_blocked_by_a_held_git_lock_is_undone_on_disk(memory: MemoryStore, repo: ResourceRepo):
    """最常见的失败是 git 锁被别的进程占着：还原不能依赖 git，必须在文件系统上完成。"""
    await memory.append(ALPHA, "- kept", ALPHA)
    lock = repo.root / ".git" / "index.lock"
    lock.write_text("")
    try:
        with pytest.raises(RuntimeError, match="index.lock"):
            await memory.append(ALPHA, "- blocked", ALPHA)
        assert memory.read(ALPHA) == "- kept\n"
    finally:
        lock.unlink()

    await memory.append(ALPHA, "- blocked", ALPHA)
    assert memory.read(ALPHA) == "- kept\n\n- blocked\n"


async def test_a_change_made_outside_the_tools_is_recorded_before_the_next_write(repo: ResourceRepo):
    """agent 用 shell 直接改了资源文件：下一次写入前单独记一版，不并进下一个作者的提交。"""
    path = repo.owner_dir(ALPHA) / "MEMORY.md"
    await repo.apply({path: "first\n"}, "memory", ALPHA)
    path.write_text("first\nedited by shell\n")

    await repo.apply({path: "first\nedited by shell\nsecond\n"}, "memory", "user")

    newest, outside, _ = await repo.history(path, 10)
    assert (outside.author, outside.message) == (
        "muliao",
        "reconcile changes made outside the resource tools",
    )
    assert newest.author == "user"
    assert await repo.read_at(outside.rev, path) == "first\nedited by shell\n"


async def test_editing_a_skill_replaces_or_appends_to_its_body_and_keeps_name_and_description(
    repo: ResourceRepo,
):
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "step one\nstep two", ALPHA)

    await skills.edit(ALPHA, "digest", ALPHA, old="step two", new="step 2")
    await skills.edit(ALPHA, "digest", ALPHA, new="## pitfalls\n- none")

    skill = skills.read(ALPHA, "digest")
    assert (skill.meta.name, skill.meta.description) == ("digest", "When digesting")
    assert skill.body == "step one\nstep 2\n\n## pitfalls\n- none"
    with pytest.raises(InvalidInput, match="exactly once"):
        await skills.edit(ALPHA, "digest", ALPHA, old="missing", new="x")
    with pytest.raises(NotFound):
        await skills.edit(ALPHA, "nope", ALPHA, new="x")


@pytest.mark.parametrize("owner", [ALPHA, None], ids=["private", "public"])
async def test_editing_a_skill_can_change_only_its_description_or_body_and_description_in_one_revision(
    repo: ResourceRepo, owner: str | None
):
    skills = SkillStore(repo)
    await skills.write(owner, "digest", "When digesting", "step one\nstep two", ALPHA)

    meta = await skills.edit(owner, "digest", ALPHA, description="  When summarizing a channel  ")
    assert meta.description == "When summarizing a channel"
    skill = skills.read(owner, "digest")
    assert (skill.meta.description, skill.body) == ("When summarizing a channel", "step one\nstep two")

    before = len(await repo.history(skill.meta.path, 10))
    await skills.edit(owner, "digest", ALPHA, old="step two", new="step 2", description="When digesting")
    skill = skills.read(owner, "digest")
    assert (skill.meta.description, skill.body) == ("When digesting", "step one\nstep 2")
    assert len(await repo.history(skill.meta.path, 10)) == before + 1


@pytest.mark.parametrize("owner", [ALPHA, None], ids=["private", "public"])
async def test_editing_a_skill_rejects_invalid_combinations_without_changing_it(
    repo: ResourceRepo, owner: str | None
):
    skills = SkillStore(repo)
    await skills.write(owner, "digest", "When digesting", "step\nstep", ALPHA)

    with pytest.raises(InvalidInput, match="nothing to change"):
        await skills.edit(owner, "digest", ALPHA)
    with pytest.raises(InvalidInput, match="description must not be empty"):
        await skills.edit(owner, "digest", ALPHA, new="more", description="  ")
    with pytest.raises(InvalidInput, match="exactly once"):
        await skills.edit(owner, "digest", ALPHA, old="step", new="x", description="New")

    skill = skills.read(owner, "digest")
    assert (skill.meta.description, skill.body) == ("When digesting", "step\nstep")


async def test_files_outside_the_resource_layout_never_enter_history(repo: ResourceRepo, tmp_path: Path):
    """agent 在 skill 目录里放的杂物（不在后缀白名单、隐藏文件、虚拟环境与依赖目录里的脚本、超限文件）不进历史；
    一个没有提交的嵌套仓库（git add 会失败）也不影响任何写入。"""
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    (skill_dir / "notes.tmp").write_text("agent's own scratch")
    (skill_dir / "tool.py.tmp").write_text("agent's own scratch")
    (skill_dir / ".env").write_text("TOKEN=x")
    (skill_dir / "bin").write_bytes(b"\x7fELF")
    (skill_dir / "big.json").write_text("x" * (MAX_EXTERNAL_FILE_BYTES + 1))
    for deps in (
        ".venv/lib",
        "node_modules/pkg",
        "venv/lib",
        "__pycache__",
        "env/site-packages",
        "sub/node_modules",
    ):
        (skill_dir / deps).mkdir(parents=True)
        (skill_dir / deps / "mod.py").write_text("x = 1\n")
    nested = skill_dir / "clone"
    nested.mkdir()
    await asyncio.to_thread(_git, "init", "-q", str(nested))
    (nested / "cloned.py").write_text("x = 1\n")
    # 有提交的嵌套仓库（git clone 的情形）也不进历史，不变成 gitlink
    cloned = skill_dir / "cloned"
    cloned.mkdir()
    await asyncio.to_thread(_git, "init", "-q", str(cloned))
    (cloned / "lib.py").write_text("x = 1\n")
    await asyncio.to_thread(_git, "-C", str(cloned), "add", "lib.py")
    await asyncio.to_thread(
        _git, "-C", str(cloned), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"
    )

    await repo.apply({repo.owner_dir(ALPHA) / "MEMORY.md": "fact\n"}, "memory", ALPHA)
    await repo.recover()

    tracked = (await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).split()
    assert sorted(tracked) == [f"agents/{ALPHA}/MEMORY.md", f"agents/{ALPHA}/skills/digest/SKILL.md"]
    # recover 只删资源文件写了一半的 .tmp，agent 自己的临时文件不动
    assert (skill_dir / "notes.tmp").exists()
    assert (skill_dir / "tool.py.tmp").exists()


async def test_text_attachments_in_a_skill_directory_are_versioned(repo: ResourceRepo):
    """skill 目录里的文本附件（含子目录）由 reconcile 收进历史，可以读旧版本写回；删除 skill 连附件一起删。"""
    skills = SkillStore(repo)
    for owner in (ALPHA, None):
        await skills.write(owner, "digest", "When digesting", "steps", ALPHA)
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    script = skill_dir / "scripts" / "fetch.py"
    script.parent.mkdir()
    script.write_text("print(1)\n")
    (skill_dir / "run.sh").write_text("echo hi\n")
    (repo.owner_dir(None) / "skills" / "digest" / "helper.py").write_text("x = 1\n")

    await repo.recover()
    tracked = set((await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).split())
    assert {
        f"agents/{ALPHA}/skills/digest/scripts/fetch.py",
        f"agents/{ALPHA}/skills/digest/run.sh",
        "public/skills/digest/helper.py",
    } <= tracked

    first = (await repo.history(script, 10))[0].rev
    script.write_text("print(2)\n")
    await repo.recover()
    assert len(await repo.history(script, 10)) == 2
    # 旧版本读得出来，可以写回（agent 用文件工具写回，再由 reconcile 记一版）
    script.write_text(await repo.read_at(first, script))
    await repo.recover()
    assert script.read_text() == "print(1)\n"
    assert len(await repo.history(script, 10)) == 3
    # 删掉一个附件也记一版
    (skill_dir / "run.sh").unlink()
    await repo.recover()
    assert f"agents/{ALPHA}/skills/digest/run.sh" not in (
        await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")
    )

    await skills.delete(ALPHA, "digest", ALPHA)
    tracked = (await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).split()
    assert not [p for p in tracked if p.startswith(f"agents/{ALPHA}/skills/digest/")]
    assert not skill_dir.exists()


async def test_an_oversized_attachment_is_left_out_with_one_warning(repo: ResourceRepo, caplog):
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    big = repo.owner_dir(ALPHA) / "skills" / "digest" / "data.csv"
    big.write_text("x" * (MAX_EXTERNAL_FILE_BYTES + 1))

    for extra in range(3):
        # 继续变大也只记一次
        big.write_text("x" * (MAX_EXTERNAL_FILE_BYTES + 1 + extra))
        await repo.recover()
    assert not await repo.history(big, 10)
    warned = [r for r in caplog.records if r.getMessage().startswith("resource file too large")]
    assert len(warned) == 1

    # 缩到上限以内就照常进历史
    big.write_text("x\n")
    await repo.recover()
    assert len(await repo.history(big, 10)) == 1
    # 已在历史里的变得超限：历史停在旧版本，之后的写入也不把它带进提交
    big.write_text("y" * (MAX_EXTERNAL_FILE_BYTES + 1))
    await repo.recover()
    await skills.write(ALPHA, "digest", "When digesting", "more steps", ALPHA)
    assert len(await repo.history(big, 10)) == 1
    assert await repo.read_at((await repo.history(big, 1))[0].rev, big) == "x\n"


async def test_attachment_paths_are_matched_literally(repo: ResourceRepo):
    """交给 git 的路径按字面匹配：叫 `*.py` 的文件不会把排除目录里的、超限的文件带进历史。"""
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    tracked_big = skill_dir / "a.py"
    tracked_big.write_text("x = 1\n")
    await repo.recover()
    tracked_big.write_text("y" * (MAX_EXTERNAL_FILE_BYTES + 1))
    (skill_dir / ".venv" / "lib").mkdir(parents=True)
    (skill_dir / ".venv" / "lib" / "x.py").write_text("x = 1\n")
    (skill_dir / "*.py").write_text("star = 1\n")

    await repo.recover()

    tracked = (await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).splitlines()
    assert f"agents/{ALPHA}/skills/digest/*.py" in tracked
    assert not [p for p in tracked if ".venv" in p]
    assert await repo.read_at((await repo.history(tracked_big, 1))[0].rev, tracked_big) == "x = 1\n"


async def test_running_git_on_no_paths_is_refused(repo: ResourceRepo):
    """空的路径列表对 git 是整棵树：拒绝，不让排除的、超限的文件进历史。"""
    with pytest.raises(ValueError, match="at least one path"):
        await repo._git_on_paths([], "add", "-A")


async def test_many_attachments_do_not_exceed_the_command_line(repo: ResourceRepo, monkeypatch):
    """路径经 stdin 交给 git：未提交的附件再多，写入也不会因参数过长而失败。"""
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    vendor = repo.owner_dir(ALPHA) / "skills" / "digest" / "vendor"
    vendor.mkdir()
    for i in range(300):
        (vendor / f"m{i}.js").write_text("x\n")
    real_git = ResourceRepo._git_bytes

    async def short_command_line(self, *args, **kwargs):
        # 模拟很小的 ARG_MAX：放得下固定的 pathspec，放不下上百个路径
        if len(args) > len(RESOURCE_PATHSPEC) + 20:
            raise OSError(7, "Argument list too long")
        return await real_git(self, *args, **kwargs)

    monkeypatch.setattr(ResourceRepo, "_git_bytes", short_command_line)
    await repo.apply({repo.owner_dir(ALPHA) / "MEMORY.md": "fact\n"}, "memory", ALPHA)
    tracked = (await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).split()
    assert f"agents/{ALPHA}/skills/digest/vendor/m299.js" in tracked


async def test_an_unreadable_attachment_does_not_block_other_changes(repo: ResourceRepo, caplog, monkeypatch):
    """一起暂存失败时逐个暂存：坏的那个留在历史外并记 warning，其余照常提交，写入不失败。"""
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    bad = skill_dir / "bad.py"
    bad.write_text("x = 1\n")
    (skill_dir / "good.py").write_text("y = 2\n")
    real_git = ResourceRepo._git_bytes

    async def unreadable(self, *args, stdin=None, **kwargs):
        if args[0] == "add" and (any(a.endswith("bad.py") for a in args) or b"bad.py" in (stdin or b"")):
            raise RuntimeError("git add failed: error: open(bad.py): Permission denied")
        return await real_git(self, *args, stdin=stdin, **kwargs)

    monkeypatch.setattr(ResourceRepo, "_git_bytes", unreadable)
    await repo.apply({repo.owner_dir(ALPHA) / "MEMORY.md": "fact\n"}, "memory", ALPHA)
    warned = [r for r in caplog.records if r.getMessage().startswith("resource file cannot be staged")]
    assert [r.path for r in warned] == [f"agents/{ALPHA}/skills/digest/bad.py"]

    tracked = (await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).split()
    assert f"agents/{ALPHA}/skills/digest/good.py" in tracked
    assert f"agents/{ALPHA}/skills/digest/bad.py" not in tracked
    assert f"agents/{ALPHA}/MEMORY.md" in tracked


async def test_every_file_the_stores_write_is_in_the_resource_layout(container, make_agent):
    """STORE_FILES（RESOURCE_FILES 的一部分）与各 store 的文件名必须一致：不在布局里的资源文件，外部写入就不会被收敛。"""
    from muliao.agents.api import McpTransport, mcp_config

    agent = (await make_agent(ALPHA)).id
    await container.memory.append(agent, "fact", agent)
    await container.memory.append(None, "public rules", "user")
    for owner in (agent, None):
        await container.skills.write(owner, "digest", "When digesting", "steps", "user")
        spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
        await container.mcp.add(owner, "echo", spec, probe=False, author="user")

    root = container.resources.root
    files = (await asyncio.to_thread(_git, "-C", str(root), "ls-files")).split()
    in_layout = (await asyncio.to_thread(_git, "-C", str(root), "ls-files", "--", *RESOURCE_PATHSPEC)).split()
    assert len(files) == 6
    assert in_layout == files


@pytest.mark.parametrize(
    "outside",
    ["edit", "delete", "new skill", "path with space"],
)
async def test_reconcile_records_each_kind_of_outside_change(repo: ResourceRepo, outside: str):
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    skill = repo.owner_dir(ALPHA) / "skills" / "digest" / "SKILL.md"
    target = {
        "edit": skill,
        "delete": skill,
        "new skill": repo.owner_dir(ALPHA) / "skills" / "fresh" / "SKILL.md",
        "path with space": repo.owner_dir(ALPHA) / "skills" / "a b" / "SKILL.md",
    }[outside]
    if outside == "delete":
        target.unlink()
    else:
        target.parent.mkdir(exist_ok=True)
        target.write_text("---\nname: x\ndescription: y\n---\nby shell\n")

    await repo.recover()

    [revision, *_] = await repo.history(target, 10)
    assert revision.author == "muliao"
    assert await asyncio.to_thread(_git, "-C", str(repo.root), "status", "--porcelain") == ""


async def test_recover_commits_a_rename_left_staged_by_a_killed_reconcile(repo: ResourceRepo):
    """进程死在 reconcile 的 add 与 commit 之间、暂存区里留下一次重命名：之后的收敛不能被它卡住。"""
    skills = SkillStore(repo)
    await skills.write(ALPHA, "old", "When digesting", "steps", ALPHA)
    old_dir = repo.owner_dir(ALPHA) / "skills" / "old"
    old_dir.rename(old_dir.with_name("new"))
    await asyncio.to_thread(_git, "-C", str(repo.root), "add", "-A")

    await repo.recover()

    tracked = (await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).split()
    assert tracked == [f"agents/{ALPHA}/skills/new/SKILL.md"]


async def test_a_symlink_named_like_a_partial_write_does_not_stop_startup(repo: ResourceRepo, tmp_path: Path):
    await repo.ensure()
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    skill_dir.mkdir(parents=True)
    (skill_dir / "cache.tmp").symlink_to(tmp_path)
    (skill_dir / "SKILL.md.tmp").write_text("half written")

    await repo.recover()

    assert (skill_dir / "cache.tmp").is_symlink()
    assert not (skill_dir / "SKILL.md.tmp").exists()


async def test_writing_a_skill_whose_directory_is_a_nested_repository_fails_instead_of_going_unversioned(
    repo: ResourceRepo,
):
    skills = SkillStore(repo)
    await repo.ensure()
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "cloned"
    skill_dir.mkdir(parents=True)
    await asyncio.to_thread(_git, "init", "-q", str(skill_dir))

    with pytest.raises(RuntimeError, match="nested repository"):
        await skills.write(ALPHA, "cloned", "When digesting", "steps", ALPHA)
    # 「报错」等于「没有生效」
    assert not (skill_dir / "SKILL.md").exists()


@pytest.mark.parametrize(
    ("staged", "then"),
    [
        ("new", "delete"),  # AD：暂存的新文件又被删掉（如写入失败后回滚时 git reset 也失败）
        ("edit", "edit"),  # MM
        ("edit", "delete"),  # MD
    ],
)
async def test_reconcile_converges_from_any_mix_of_staged_and_unstaged_changes(
    repo: ResourceRepo, staged: str, then: str
):
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    skill_dir = repo.owner_dir(ALPHA) / "skills" / ("fresh" if staged == "new" else "digest")
    skill_dir.mkdir(exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text("---\nname: x\ndescription: y\n---\nstaged\n")
    await asyncio.to_thread(_git, "-C", str(repo.root), "add", "-A")
    if then == "delete":
        path.unlink()
    else:
        path.write_text("---\nname: x\ndescription: y\n---\nedited again\n")

    await repo.recover()

    assert await asyncio.to_thread(_git, "-C", str(repo.root), "status", "--porcelain") == ""
    # 收敛后正常写入照常进行
    assert await repo.apply({repo.owner_dir(ALPHA) / "MEMORY.md": "fact\n"}, "memory", ALPHA) is not None


async def test_contents_at_reads_every_revision_in_one_batch(repo: ResourceRepo):
    """历史里每一版的内容一次读出（cat-file --batch），与版本一一对应：多行、非 ASCII、空内容都按字节长度切分。"""
    path = repo.root / "public" / "prompt.md"
    for content in ("第一版\n两行\n", "", "x"):
        await repo.apply({path: content}, "edit", "user")
    revisions = await repo.history(path, 10)
    assert await repo.contents_at([r.rev for r in revisions], path) == ["x", "", "第一版\n两行\n"]
    assert await repo.contents_at([], path) == []

    other = repo.root / "public" / "other.md"
    with pytest.raises(NotFound):
        await repo.contents_at([revisions[0].rev], other)


async def test_a_write_for_an_unavailable_owner_writes_and_commits_nothing(repo: ResourceRepo):
    """owner 由路径推出、默认检查：store 不必记得传，漏不掉。"""
    checked: list[str | None] = []

    async def refuse(owner: str | None) -> None:
        checked.append(owner)
        if owner is not None:
            raise NotFound("agent gone")

    guarded = ResourceRepo(repo.root, refuse)
    path = repo.owner_dir(ALPHA) / "MEMORY.md"
    head = _git("-C", str(repo.root), "rev-parse", "HEAD")

    with pytest.raises(NotFound):
        await guarded.apply({path: "x"}, "memory", ALPHA)
    with pytest.raises(NotFound):
        await guarded.update(path, lambda _c: "x", "memory", ALPHA)
    with pytest.raises(NotFound):
        await SkillStore(guarded).write(ALPHA, "s", "d", "x", ALPHA)
    # 一次写入涉及的每个 owner 都查：公共的放行不足以让同次的私有写入通过
    with pytest.raises(NotFound):
        await guarded.apply({repo.root / "public" / "MEMORY.md": "p", path: "x"}, "both", "user")

    assert checked == [ALPHA, ALPHA, ALPHA, None, ALPHA]
    assert not repo.owner_dir(ALPHA).exists()
    assert _git("-C", str(repo.root), "rev-parse", "HEAD") == head
    # 显式不查的只有创建与删除 agent
    assert await guarded.apply({path: "x"}, "create agent", "user", unchecked=True) is not None


async def test_a_path_outside_every_owner_directory_is_not_a_resource(repo: ResourceRepo):
    with pytest.raises(ValueError, match="not inside an owner directory"):
        await repo.apply({repo.root / "stray.md": "x"}, "stray", "user")
    with pytest.raises(ValueError, match="not inside an owner directory"):
        await repo.apply({repo.root / "agents" / "..x" / "MEMORY.md": "x"}, "stray", "user")


async def test_the_owner_check_runs_inside_the_repository_lock(repo: ResourceRepo):
    """检查与写入之间插不进别的写入（例如删除 agent 删掉它的目录）：检查在锁里执行。"""
    path = repo.owner_dir(ALPHA) / "MEMORY.md"
    checking, release = anyio.Event(), anyio.Event()
    order: list[str] = []

    async def slow_check(owner: str | None) -> None:
        if owner is None:
            return
        checking.set()
        await release.wait()
        order.append("checked")

    guarded = ResourceRepo(repo.root, slow_check)

    async def other_write() -> None:
        await checking.wait()
        await guarded.apply({repo.owner_dir(ALPHA): None}, "delete agent", "user", unchecked=True)
        order.append("deleted")

    async with anyio.create_task_group() as group:
        group.start_soon(other_write)
        group.start_soon(partial(guarded.apply, {path: "fact\n"}, "memory", ALPHA))
        await checking.wait()
        await anyio.sleep(0.1)
        assert order == []
        release.set()

    assert order == ["checked", "deleted"] and not path.exists()
