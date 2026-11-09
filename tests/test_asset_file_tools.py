"""文件工具写资源目录（#500）：写之前按读树的规则校验格式，不合格拒绝并说明；write_file 整体覆盖已有的资源文件要先读过、
之后没被改过；资源文件原子写入。"""

import json
import os

from tests.test_tools import run_tools

SKILL_MD = "---\ndescription: does things\n---\nbody\n"


async def _setup(container, make_agent):
    me = await make_agent("alice", model="tools")
    await container.skills.write("helper", "helps", "body", actor="user", owner=me.id)
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
        ],
    )
    assert "frontmatter must contain a string 'description'" in outputs[0]
    assert "section 'Skills'" in outputs[0] and "assets-files skill" in outputs[0]
    assert "agent 'alice''s asset files" in outputs[0]
    assert "over the limit of 4000" in outputs[1] and "section 'Memory'" in outputs[1]
    assert "not valid JSON" in outputs[2] and "section 'MCP servers'" in outputs[2]
    assert "not a place for an asset file" in outputs[3]
    assert "nothing was written" in outputs[4]
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
    assert (await container.agents.get(me.id)).description == "new job"


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
    assert await container.vault.resolve_many(me.id, ["TOKEN"]) == {"TOKEN": "s3cret"}
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
    # 校验要 await：等待期间别处写进来的，write_file 与 edit_file 都不能盖掉
    me, _tree, mine = await _setup(container, make_agent)
    memory = mine / "memory.md"
    memory.write_text("note\n")
    vet = container.assets.vet

    async def racing_vet(path, text):
        await vet(path, text)
        if path == memory:
            memory.write_text(memory.read_text() + "theirs\n")
            st = memory.stat()
            os.utime(memory, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    monkeypatch.setattr(container.assets, "vet", racing_vet)
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
