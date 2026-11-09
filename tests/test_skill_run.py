"""secret 的 allow_shell 与 skill_run（#528）：不允许 shell 的 key，agent 自己写的命令（run_shell、precheck）拿不到，
只能经 skill 目录里的脚本（skill_run）用；输出里的明文照样替换成 [secret:KEY]。"""

import os

from tests.conftest import share
from tests.test_jobs import wait_until_done
from tests.test_tools import run_tools

SCRIPT = '#!/bin/sh\necho "args: $*"\necho "key is $DEPLOY_KEY"\n'


def make_skill(container, owner: str, name: str = "ops", script: str = SCRIPT, executable: bool = True):
    base = container.settings.assets_dir / owner / "skills" / name
    (base / "scripts").mkdir(parents=True)
    (base / "SKILL.md").write_text(
        f"---\ndescription: deploy things\nname: {name}\n---\n\nRun scripts/deploy.sh\n"
    )
    path = base / "scripts" / "deploy.sh"
    path.write_text(script)
    if executable:
        os.chmod(path, 0o755)
    return base


async def test_a_key_not_allowed_in_shell_only_reaches_skill_scripts(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    await container.vault.set("DEPLOY_KEY", "s3cr3t-deploy", "", actor=me.id, owner=me.id, allow_shell=False)
    make_skill(container, "alpha")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("run_shell", {"command": "echo $DEPLOY_KEY", "timeout": 10, "secrets": ["DEPLOY_KEY"]}),
            (
                "skill_run",
                {
                    "skill": "ops",
                    "script": "scripts/deploy.sh",
                    "args": ["prod", "a b"],
                    "timeout": 10,
                    "secrets": ["DEPLOY_KEY"],
                },
            ),
        ],
    )
    assert "not allowed in shell commands" in outputs[0] and "s3cr3t" not in outputs[0]
    assert outputs[1] == "exit_code: 0\nargs: prod a b\nkey is [secret:DEPLOY_KEY]\n"


async def test_skill_run_checks_the_skill_and_the_script(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    await make_agent("beta")
    make_skill(container, "beta", name="theirs")
    make_skill(container, "alpha", name="plain", executable=False)
    outside = container.settings.assets_dir / "alpha" / "skills" / "plain" / "scripts" / "out.sh"
    os.symlink("/bin/echo", outside)
    calls = [
        ("skill_run", {"skill": "theirs", "script": "scripts/deploy.sh", "timeout": 10}),
        ("skill_run", {"skill": "plain", "script": "../../SKILL.md", "timeout": 10}),
        ("skill_run", {"skill": "plain", "script": "scripts/out.sh", "timeout": 10}),
        ("skill_run", {"skill": "plain", "script": "scripts/missing.sh", "timeout": 10}),
        ("skill_run", {"skill": "plain", "script": "scripts/deploy.sh", "timeout": 10}),
    ]
    outputs = await run_tools(container, models, me.id, calls)
    assert "skill 'theirs' not found among the skills you can use" in outputs[0]
    assert "inside the skill's directory" in outputs[1]
    assert "inside the skill's directory" in outputs[2]
    assert "has no script 'scripts/missing.sh'" in outputs[3]
    assert "is not executable" in outputs[4]


async def test_a_background_skill_run_redacts_its_log(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    await container.vault.set("DEPLOY_KEY", "s3cr3t-deploy", "", actor=me.id, owner=me.id, allow_shell=False)
    make_skill(container, "alpha")
    args = {"skill": "ops", "script": "scripts/deploy.sh", "secrets": ["DEPLOY_KEY"], "background": True}
    [started] = await run_tools(container, models, me.id, [("skill_run", args)])
    job_id = int(started.split()[3].rstrip(";"))
    job = await wait_until_done(container, job_id)
    log = (container.agents.workspace(me.id) / job.log_path).read_text()
    assert log == "args: \nkey is [secret:DEPLOY_KEY]\n"


async def test_vault_set_keeps_allow_shell_unless_told(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("vault_set", {"key": "K", "value": "v1", "allow_shell": False}),
            ("vault_set", {"key": "K", "value": "v2"}),
            ("vault_set", {"key": "NEW", "value": "v"}),
        ],
    )
    assert outputs == [
        "stored secret K (allow_shell: false)",
        "stored secret K (allow_shell: false)",
        "stored secret NEW (allow_shell: true)",
    ]
    assert not (await container.vault.get("K")).allow_shell
    assert await container.vault.resolve_many(me.id, ["K"], shell=False) == {"K": "v2"}


async def test_allow_shell_can_be_edited_by_hand(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    await container.vault.set("K", "v", "", actor=me.id, owner=me.id)
    path = container.settings.assets_dir / "alpha" / "secrets" / "K.toml"
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "edit_file",
                {"path": str(path), "old_text": "allow_shell = true", "new_text": "allow_shell = false"},
            )
        ],
    )
    assert outputs[0].startswith("edited")
    assert not (await container.vault.get("K")).allow_shell


async def test_a_precheck_cannot_use_a_key_not_allowed_in_shell(container, make_agent, models):
    me = await make_agent("alpha", model="tools")
    await container.vault.set("K", "v", "", actor="user", owner=None, allow_shell=False)
    await share(container, "use:secret:K")
    args = {
        "name": "watch",
        "kind": "schedule",
        "prompt": "look",
        "config": {"cron": "0 9 * * *"},
        "precheck": {"command": "echo $K", "secrets": ["K"]},
    }
    [output] = await run_tools(container, models, me.id, [("trigger_create", args)])
    assert "not allowed in shell commands" in output


async def test_the_api_sets_and_shows_allow_shell(client, container):
    from tests.conftest import login

    await login(client)
    resp = await client.put("/api/items/secret/K", json={"value": "v", "allow_shell": False})
    assert resp.status_code == 204
    assert (await client.get("/api/items/secret/K")).json()["allow_shell"] is False
    [item] = [i for i in (await client.get("/api/items")).json() if i["kind"] == "secret"]
    assert item["allow_shell"] is False
    resp = await client.put("/api/items/secret/K", json={"value": "v2"})
    assert (await client.get("/api/items/secret/K")).json()["allow_shell"] is False
