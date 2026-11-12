"""文件工具写资源目录（#500）：写之前按读树的规则校验格式，不合格拒绝并说明；write_file 整体覆盖已有的资源文件要先读过、
之后没被改过；资源文件原子写入。"""

import json
import os

import pytest

from muliao.agents.api import RoleSpec
from muliao.agents.service import AgentPatch
from muliao.errors import Conflict, InvalidInput
from tests.test_tools import run_tools

SKILL_MD = "---\ndescription: does things\n---\nbody\n"


async def _setup(container, make_agent):
    me = await make_agent("alice", model="tools")
    await container.skills.write("helper", "helps", "body", version=None, owner=me.id)
    tree = container.settings.assets_dir
    return me, tree, tree / "alice"


async def test_one_edit_file_changes_a_skill_attachment(container, make_agent, models):
    me, _tree, mine = await _setup(container, make_agent)
    ref = mine / "skills/helper/references/notes.md"
    ref.parent.mkdir(parents=True)
    ref.write_text("step one\n")
    outputs = await run_tools(
        container, models, me.id, [("edit_file", {"path": str(ref), "old_text": "one", "new_text": "two"})]
    )
    assert outputs[0].startswith(f"edited {ref}")
    assert ref.read_text() == "step two\n"
    # 原子写入不留临时文件
    assert sorted(p.name for p in ref.parent.iterdir()) == ["notes.md"]


async def test_wrong_formats_are_refused_with_the_reason_and_the_skill_section(container, make_agent, models):
    me, _tree, mine = await _setup(container, make_agent)
    skill_md = mine / "skills/helper/SKILL.md"
    before = skill_md.read_text()
    (mine / "memory.md").write_text("note\n")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": str(mine / "skills/new-one/SKILL.md"), "content": "no frontmatter\n"}),
            ("edit_file", {"path": str(mine / "memory.md"), "old_text": "note", "new_text": "x" * 4001}),
            ("write_file", {"path": str(mine / "mcp/srv.json"), "content": "{not json"}),
            ("write_file", {"path": str(mine / "notes.txt"), "content": "hi"}),
            ("edit_file", {"path": str(skill_md), "old_text": "description: helps", "new_text": "title: x"}),
            # 放文件的资源子目录本身也不能写成文件（曾经漏判、校验返回空，类型检查发现）
            ("write_file", {"path": str(mine / "mcp"), "content": "{}"}),
        ],
    )
    assert "frontmatter must contain a string 'description'" in outputs[0]
    assert "section 'Skills'" in outputs[0] and "assets-files skill" in outputs[0]
    assert "agent 'alice''s asset files" in outputs[0]
    assert "over the limit of 4000" in outputs[1] and "section 'Memory'" in outputs[1]
    assert "not valid JSON" in outputs[2] and "section 'MCP servers'" in outputs[2]
    assert "not a place for an asset file" in outputs[3]
    assert "nothing was written" in outputs[4]
    assert "is a directory, not a file" in outputs[5] and "section 'MCP servers'" in outputs[5]
    assert not (mine / "skills/new-one").exists() and not (mine / "mcp").exists()
    assert not (mine / "notes.txt").exists()
    assert (mine / "memory.md").read_text() == "note\n"
    assert skill_md.read_text() == before


async def test_valid_asset_files_are_written(container, make_agent, models):
    me, _tree, mine = await _setup(container, make_agent)
    mcp = {"transport": "http", "url": "https://example.com/mcp"}
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": str(mine / "skills/new-one/SKILL.md"), "content": SKILL_MD}),
            ("write_file", {"path": str(mine / "mcp/srv.json"), "content": json.dumps(mcp)}),
            ("write_file", {"path": str(mine / "skills/new-one/.draft"), "content": "anything"}),
        ],
    )
    assert all(o.startswith("created ") for o in outputs), outputs
    assert "new-one" in [s.name for s in await container.skills.index(me.id)]


async def test_names_taken_elsewhere_are_refused(container, make_agent, models):
    await _setup(container, make_agent)
    bob = await make_agent("bob", model="tools")
    mine = container.settings.assets_dir / "bob"
    outputs = await run_tools(
        container,
        models,
        bob.id,
        [("write_file", {"path": str(mine / "skills/helper/SKILL.md"), "content": SKILL_MD})],
    )
    assert "already exists" in outputs[0]
    assert not (mine / "skills/helper").exists()


async def test_agent_toml_keeps_its_id(container, make_agent, models):
    me, _tree, mine = await _setup(container, make_agent)
    toml = mine / "agent.toml"
    before = toml.read_text()
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("edit_file", {"path": str(toml), "old_text": f'id = "{me.id}"', "new_text": 'id = "other"'}),
            ("edit_file", {"path": str(toml), "old_text": 'description = "test agent"', "new_text": "bad ="}),
            (
                "edit_file",
                {
                    "path": str(toml),
                    "old_text": 'description = "test agent"',
                    "new_text": 'description = "new job"',
                },
            ),
        ],
    )
    assert "id must stay" in outputs[0]
    assert "invalid agent.toml" in outputs[1]
    assert outputs[2].startswith("edited ")
    assert toml.read_text() == before.replace("test agent", "new job")
    await container.assets.snapshot()  # 运行开始时读树，同步进 agent 的配置
    assert (await container.agents.view(me.id)).config.file.description == "new job"


async def test_access_toml_is_checked_like_the_tree_reads_it(container, make_agent, models):
    root = await make_agent("root", model="tools", roles=("admin",))
    access = container.settings.assets_dir / "platform/access.toml"
    before = access.read_text()
    outputs = await run_tools(
        container,
        models,
        root.id,
        [
            ("read_file", {"path": str(access)}),
            (
                "write_file",
                {
                    "path": str(access),
                    "content": before + '\n[roles.ops]\ndescription = "x"\nallow = ["nope"]\n',
                },
            ),
            (
                "write_file",
                {
                    "path": str(access),
                    "content": before + '\n[roles.ops]\ndescription = "x"\nallow = ["use:tool:issue_*"]\n',
                },
            ),
        ],
    )
    assert "role 'ops'" in outputs[1] and "section 'Roles and permissions'" in outputs[1]
    assert outputs[2].startswith("overwrote ")
    assert "ops" in [r.name for r in await container.distribution.list()]


async def test_secrets_are_written_only_by_vault_set(container, make_agent, models):
    me, _tree, mine = await _setup(container, make_agent)
    await container.vault.set("TOKEN", "s3cret", "old note", actor=me.id, owner=me.id)
    secret = mine / "secrets/TOKEN.toml"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": str(mine / "secrets/NEW.toml"), "content": 'ciphertext = "abc"\n'}),
            ("edit_file", {"path": str(secret), "old_text": "gAAAA", "new_text": "hAAAA"}),
            ("edit_file", {"path": str(secret), "old_text": "old note", "new_text": "new note"}),
        ],
    )
    assert "vault_set" in outputs[0] and "section 'Secrets'" in outputs[0]
    assert "ciphertext must stay" in outputs[1]
    assert outputs[2].startswith("edited ")
    assert await container.vault.resolve_many(me.id, ["TOKEN"], shell=False) == {"TOKEN": "s3cret"}
    assert (await container.vault.get("TOKEN")).description == "new note"


async def test_overwriting_an_asset_file_needs_a_fresh_read(container, make_agent, models):
    me, _tree, mine = await _setup(container, make_agent)
    memory = mine / "memory.md"
    memory.write_text("old\n")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("write_file", {"path": str(memory), "content": "mine\n"}),
            ("read_file", {"path": str(memory)}),
            ("run_shell", {"command": f"sleep 0.01; echo theirs > {memory}", "timeout": 30}),
            ("write_file", {"path": str(memory), "content": "mine\n"}),
            ("read_file", {"path": str(memory)}),
            ("write_file", {"path": str(memory), "content": "mine\n"}),
            # 刚写过的可以接着整体覆盖
            ("write_file", {"path": str(memory), "content": "mine again\n"}),
            # 资源目录以外照旧
            ("write_file", {"path": "plain.txt", "content": "a"}),
            ("write_file", {"path": "plain.txt", "content": "b"}),
        ],
    )
    assert "read it with read_file first" in outputs[0]
    assert "was changed after you read it" in outputs[3]
    assert outputs[5].startswith("overwrote ") and outputs[6].startswith("overwrote ")
    assert memory.read_text() == "mine again\n"
    assert outputs[8].startswith("overwrote ")


async def test_an_oversized_memory_can_be_shortened_step_by_step(container, make_agent, models):
    me, _tree, mine = await _setup(container, make_agent)
    memory = mine / "memory.md"
    memory.write_text("a" * 3000 + "\n" + "b" * 2000 + "\n")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("edit_file", {"path": str(memory), "old_text": "b" * 2000, "new_text": "b" * 1500}),
            ("edit_file", {"path": str(memory), "old_text": "b" * 1500, "new_text": "b" * 1600}),
        ],
    )
    assert outputs[0].startswith("edited ")
    assert "over the limit" in outputs[1]
    assert memory.read_text().count("b") == 1500


async def test_a_write_landing_while_the_check_runs_is_not_overwritten(
    container, make_agent, models, monkeypatch
):
    # 校验期间（不经写入口，如 shell）别处写进来的，write_file 与 edit_file 都不能盖掉
    from muliao.agents import asset_files

    me, _tree, mine = await _setup(container, make_agent)
    memory = mine / "memory.md"
    memory.write_text("note\n")
    vet = asset_files.vet

    def racing_vet(snapshot, path, text, *args):
        stored = vet(snapshot, path, text, *args)
        if path == memory:
            memory.write_text(memory.read_text() + "theirs\n")
            st = memory.stat()
            os.utime(memory, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return stored

    monkeypatch.setattr(asset_files, "vet", racing_vet)
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("edit_file", {"path": str(memory), "old_text": "note", "new_text": "mine"}),
            ("read_file", {"path": str(memory)}),
            ("write_file", {"path": str(memory), "content": "mine\n"}),
        ],
    )
    assert "changed while this edit was being checked" in outputs[0]
    assert "was changed after you read it" in outputs[2]
    assert memory.read_text() == "note\ntheirs\ntheirs\n"


def test_every_rejection_names_a_section_of_the_assets_skill():
    """报错里的 `section '<名>'` 必须是公共 skill assets-files 的小节标题（Section）：源码里传给 _Rejected / _new 的与各种资源
    的（KindSpec.section）小节名合起来就是 SECTIONS；SECTIONS 变了，要同步改 skill 的 `## <名>` 标题（#501）。"""
    import ast
    import inspect

    from muliao.agents import asset_files, kinds

    tree = ast.parse(inspect.getsource(asset_files))
    used = {
        call.args[-1].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id in {"_Rejected", "_new"}
        and call.args
        and isinstance(call.args[-1], ast.Constant)
    }
    assert used | {kinds.spec_of(k).section for k in kinds.StoredKind} == set(kinds.SECTIONS)
    assert kinds.SECTIONS == (
        "Layout",
        "Memory",
        "Skills",
        "MCP servers",
        "Secrets",
        "agent.toml",
        "Roles and permissions",
    )


# ---- 界面与文件工具经同一个写入口（#578）：同一份内容结论一致 ----


async def _ui(call) -> str:
    """界面那一路的结论：写成了是 ok，被拒是错误信息。"""
    try:
        await call
    except (InvalidInput, Conflict) as e:
        return f"refused: {e}"
    return "ok"


def _tool(output: str) -> str:
    return "ok" if output.startswith(("edited ", "created ", "overwrote ")) else f"refused: {output}"


async def test_the_app_and_the_file_tools_reach_the_same_conclusion(container, make_agent, models):
    root = await make_agent("root", model="tools", roles=("admin",))
    tree = container.settings.assets_dir
    memory = tree / "root" / "memory.md"
    toml = tree / "root" / "agent.toml"
    access = tree / "platform" / "access.toml"
    await container.distribution.create(RoleSpec("ops", "runs things", [], []))
    long = "a" * 3000 + "\n" + "b" * 2100

    async def memory_ui(content: str) -> str:
        info = await container.memory.read_private(root.id)
        return await _ui(container.memory.put_private(root.id, content, version=info.version))

    async def memory_tool(content: str) -> str:
        out = await run_tools(
            container,
            models,
            root.id,
            [("read_file", {"path": str(memory)}), ("write_file", {"path": str(memory), "content": content})],
        )
        return _tool(out[1])

    # 已经超长的记忆：删短一些（仍超长）两边都放行，变长两边都拒绝
    for write in (memory_ui, memory_tool):
        memory.write_text(long + "\n")
        assert await write(long[:-50]) == "ok", write
        assert (await write(long + "b")).startswith("refused"), write
    # 空白规整两边一样：存下的就是指令里显示的样子（#361）
    for write in (memory_ui, memory_tool):
        memory.write_text("x\n")
        assert await write("first  \n\n\n\nsecond\n\n") == "ok"
        assert memory.read_text() == "first\n\nsecond\n"

    # agent.toml 的描述不能空
    ui = await _ui(container.agents.update(root.id, AgentPatch(description="  "), actor="user"))
    out = await run_tools(
        container,
        models,
        root.id,
        [
            (
                "edit_file",
                {
                    "path": str(toml),
                    "old_text": 'description = "test agent"',
                    "new_text": 'description = " "',
                },
            )
        ],
    )
    assert "description must not be empty" in ui and "description must not be empty" in _tool(out[0])

    # 角色的描述不能空
    ui = await _ui(container.distribution.patch("ops", description=" "))
    out = await run_tools(
        container,
        models,
        root.id,
        [
            (
                "edit_file",
                {
                    "path": str(access),
                    "old_text": 'description = "runs things"',
                    "new_text": 'description = ""',
                },
            )
        ],
    )
    assert "description must not be empty" in ui and "description must not be empty" in _tool(out[0])

    # 新建的平台记忆名字要是 slug
    ui = await _ui(container.memory.put("Bad Name", "x", version=None))
    out = await run_tools(
        container,
        models,
        root.id,
        [("write_file", {"path": str(tree / "platform/memory/Bad Name.md"), "content": "x"})],
    )
    assert "invalid memory name" in ui and "invalid memory name" in _tool(out[0])
    # 界面上只给原因，文件工具另带上给 agent 的说明（看 assets-files 哪一节）
    assert "assets-files" not in ui and "section 'Memory'" in out[0]

    # 新加的规则拼错了（匹配不上任何资源）两边都拒绝：只有 access_file.vet 一个校验
    ui = await _ui(container.distribution.patch("ops", allow=["use:skill:no-such-skil"]))
    out = await run_tools(
        container,
        models,
        root.id,
        [
            (
                "edit_file",
                {
                    "path": str(access),
                    "old_text": "allow = []",
                    "new_text": 'allow = ["use:skill:no-such-skil"]',
                },
            )
        ],
    )
    assert "match no existing" in ui and "match no existing" in _tool(out[0])

    # 没有 SKILL.md 的 skill 目录写入时就拒绝；与 SKILL.md 一起写、或它已在时放行
    new = tree / "platform" / "skills" / "fresh"
    out = await run_tools(
        container, models, root.id, [("write_file", {"path": str(new / "run.sh"), "content": "echo"})]
    )
    assert "has no SKILL.md" in _tool(out[0]) and not new.exists()
    md = "---\ndescription: When fresh\n---\nSteps.\n"
    out = await run_tools(
        container,
        models,
        root.id,
        [
            ("write_file", {"path": str(new / "SKILL.md"), "content": md}),
            ("write_file", {"path": str(new / "run.sh"), "content": "echo"}),
        ],
    )
    assert [_tool(o) for o in out] == ["ok", "ok"]


def test_layout_maps_paths_and_assets_both_ways():
    from muliao.agents.assets import (
        PRIVATE_MEMORY_REL,
        Config,
        Entry,
        Misplaced,
        PrivateMemory,
        Shelf,
        place_of,
        rel_of,
    )
    from muliao.agents.kinds import StoredKind

    for kind, name in [
        (StoredKind.MEMORY, "trading"),
        (StoredKind.SKILL, "helper"),
        (StoredKind.MCP, "srv"),
        (StoredKind.SECRET, "TOKEN"),
    ]:
        for in_agent in (False, True):
            if kind == StoredKind.MEMORY and in_agent:
                continue
            assert place_of(rel_of(kind, name), in_agent) == Entry(kind, name)
    # 私有记忆不在货架上，没有名字
    assert place_of(PRIVATE_MEMORY_REL, True) == PrivateMemory()
    assert place_of(("skills", "helper", "SKILL.md"), False) == Entry(
        StoredKind.SKILL, "helper", ("SKILL.md",)
    )
    assert place_of(("skills",), True) == Shelf(StoredKind.SKILL)
    assert place_of(("agent.toml",), True) == Config("agent.toml")
    assert place_of(("access.toml",), False) == Config("access.toml")
    for rel, in_agent in [
        (("memory", "x.md"), True),
        (("mcp", "x.txt"), False),
        (("notes.txt",), False),
        (PRIVATE_MEMORY_REL, False),
    ]:
        with pytest.raises(Misplaced):
            place_of(rel, in_agent)
