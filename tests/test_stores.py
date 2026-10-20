import asyncio
import subprocess
from pathlib import Path

import pytest

from muliao.errors import CorruptState, InvalidInput, NotFound
from muliao.evolution.memory import MemoryFull, MemoryStore
from muliao.evolution.skills import SkillStore
from muliao.resources import RESOURCE_PATHSPEC, ResourceRepo
from tests.test_flow import drain

# agent id 的格式（与 ids.new_agent_id 一致）
ALPHA, BETA = "a1a1a1a1a1a1", "b2b2b2b2b2b2"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> ResourceRepo:
    return ResourceRepo(tmp_path / "resources")


@pytest.fixture
def skills(repo: ResourceRepo) -> SkillStore:
    return SkillStore(repo)


async def test_private_skill_shadows_public_skill_with_same_name(skills: SkillStore):
    await skills.write(None, "digest", "public digest", "public body", "user")
    await skills.write(ALPHA, "digest", "private digest", "private body", "user")
    await skills.write(None, "other", "other public", "x", "user")

    visible = {(s.name, s.scope) for s in skills.visible_to(ALPHA)}

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


def test_skill_with_mismatched_frontmatter_name_fails_loudly(skills: SkillStore, repo: ResourceRepo):
    d = repo.owner_dir(None) / "skills" / "real"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: fake\ndescription: x\n---\nbody")

    with pytest.raises(CorruptState, match="does not match directory"):
        skills.list(None)


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

    assert await vault.render("Bearer ${vault:TOKEN}", None) == "Bearer s3cret"
    assert [s.key for s in await vault.list_all()] == ["TOKEN"]
    with pytest.raises(NotFound):
        await vault.render("${vault:MISSING}", None)
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
    assert await vault.render("${vault:TOKEN}", None) == "public"
    with pytest.raises(NotFound):
        await vault.resolve_many(beta, ["ALPHA_KEY"])
    assert [(s.key, s.owner) for s in await vault.visible_to(alpha)] == [
        ("ALPHA_KEY", alpha),
        ("TOKEN", alpha),
    ]

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
    assert await container.vault.list_all() == []


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


async def test_corrupt_mcp_config_is_rejected_with_its_path_but_can_be_deleted(container):
    from muliao.runtime.mcp import McpTransport, mcp_config

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
            container.mcp.list(None)

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

    await skills.edit(ALPHA, "digest", "step two", "step 2", ALPHA)
    await skills.edit(ALPHA, "digest", "", "## pitfalls\n- none", ALPHA)

    skill = skills.read(ALPHA, "digest")
    assert (skill.meta.name, skill.meta.description) == ("digest", "When digesting")
    assert skill.body == "step one\nstep 2\n\n## pitfalls\n- none"
    with pytest.raises(InvalidInput, match="exactly once"):
        await skills.edit(ALPHA, "digest", "missing", "x", ALPHA)
    with pytest.raises(NotFound):
        await skills.edit(ALPHA, "nope", "", "x", ALPHA)


async def test_files_outside_the_resource_layout_never_enter_history(repo: ResourceRepo, tmp_path: Path):
    """agent 在 skill 目录里放的杂物不进历史；一个没有提交的嵌套仓库（git add 会失败）也不影响任何写入。"""
    skills = SkillStore(repo)
    await skills.write(ALPHA, "digest", "When digesting", "steps", ALPHA)
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    (skill_dir / "notes.tmp").write_text("agent's own scratch")
    (skill_dir / ".env").write_text("TOKEN=x")
    nested = skill_dir / "clone"
    nested.mkdir()
    await asyncio.to_thread(_git, "init", "-q", str(nested))

    await repo.apply({repo.owner_dir(ALPHA) / "MEMORY.md": "fact\n"}, "memory", ALPHA)
    await repo.recover()

    tracked = (await asyncio.to_thread(_git, "-C", str(repo.root), "ls-files")).split()
    assert sorted(tracked) == [f"agents/{ALPHA}/MEMORY.md", f"agents/{ALPHA}/skills/digest/SKILL.md"]
    # recover 只删资源文件写了一半的 .tmp，agent 自己的临时文件不动
    assert (skill_dir / "notes.tmp").exists()


async def test_every_file_the_stores_write_is_in_the_resource_layout(container, make_agent):
    """RESOURCE_FILES 与各 store 的文件名必须一致：不在布局里的资源文件，外部写入就不会被收敛。"""
    from muliao.runtime.mcp import McpTransport, mcp_config

    agent = (await make_agent(ALPHA)).id
    await container.memory.append(agent, "fact", agent)
    await container.prompts.set(None, "public rules", "user")
    for owner in (agent, None):
        await container.skills.write(owner, "digest", "When digesting", "steps", "user")
        spec = mcp_config(transport=McpTransport.HTTP, url="http://x")
        await container.mcp.add(owner, "echo", spec, probe=False, author="user")

    root = container.resources.root
    files = (await asyncio.to_thread(_git, "-C", str(root), "ls-files")).split()
    in_layout = (await asyncio.to_thread(_git, "-C", str(root), "ls-files", "--", *RESOURCE_PATHSPEC)).split()
    assert len(files) == 7
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


def test_stored_responses_carry_the_kind_that_step_counting_queries():
    import json

    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    from muliao.runtime.codec import KIND_PATH, RESPONSE_KIND, dump_message

    field = KIND_PATH.removeprefix("$.")
    assert json.loads(dump_message(ModelResponse(parts=[TextPart("x")])))[field] == RESPONSE_KIND
    assert json.loads(dump_message(ModelRequest(parts=[UserPromptPart("x")])))[field] != RESPONSE_KIND
