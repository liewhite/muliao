"""写沙箱（#416）：执行层的适配器按接口测（每个系统的适配器跑同一组用例，现在只有 macOS），领域层（可写路径怎么算、文件
工具的检查、被拒说明）与端到端（run_shell、write_file 对 agent 的样子）。"""

import sys
from pathlib import Path

import pytest

from muliao.agents.api import AgentPatch, Rules, WriteScope
from muliao.agents.assets import load_tree
from muliao.errors import Forbidden, InvalidInput
from muliao.platform.sandbox import Category, Confinement, Sandbox, WritablePaths, real_forms, sandbox_for
from muliao.platform.shell import run_bash
from muliao.workspace.writable import DEFAULT_WRITABLE, Confiner, denial_note, reach
from tests.test_tools import run_tools

ADAPTERS = [pytest.param("darwin", marks=pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"))]


@pytest.fixture(params=ADAPTERS)
def platform(request) -> str:
    return request.param


def _confinement(platform: str, rules: list[tuple[bool, Path]], mode="enforce") -> Confinement:
    return Confinement(
        sandbox_for(mode, Path.home(), platform),
        WritablePaths.of(rules, "agent-x"),
        Path.home(),
        lambda p: f"[If this was a write: {p} is outside; grant 'write:path:<directory>']",
    )


async def _sh(confinement: Confinement, cwd: Path, command: str):
    return await run_bash(
        command, cwd=cwd, secrets={}, timeout=30, confinement=confinement, merge_stderr=True
    )


# ---- 可写路径（与系统无关） ----


def test_the_last_rule_covering_a_path_decides_and_uncovered_paths_are_not_writable(tmp_path):
    paths = WritablePaths.of(
        [(True, tmp_path / "a"), (False, tmp_path / "a/b"), (True, tmp_path / "a/b/c")], "x"
    )
    assert paths.permits(tmp_path / "a/file")
    assert not paths.permits(tmp_path / "a/b/file")
    assert paths.permits(tmp_path / "a/b/c/deep/file")
    assert not paths.permits(tmp_path / "other")
    # 只按路径分量算上级，不按字符串前缀
    assert not paths.permits(tmp_path / "ab")


def test_paths_are_judged_where_they_really_point(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok/escape").symlink_to(tmp_path / "real")
    paths = WritablePaths.of([(True, tmp_path / "ok")], "x")
    assert not paths.permits(tmp_path / "ok/escape/f")
    # 规则写的是链接：放开的是它指向的目录
    assert WritablePaths.of([(True, tmp_path / "link")], "x").permits(tmp_path / "real/f")


# ---- 适配器（每个系统同一组用例） ----


async def test_commands_and_their_children_write_only_inside_the_writable_paths(platform, tmp_path):
    ok, no = tmp_path / "ok", tmp_path / "no"
    ok.mkdir(), no.mkdir()
    c = _confinement(platform, [(True, ok), (True, Path("/dev"))])
    result = await _sh(
        c,
        ok,
        f"echo a > {ok}/a; echo b > {no}/b; python3 -c \"open('{no}/c', 'w')\"; "
        f"(nohup sh -c 'echo d > {no}/d' >/dev/null 2>&1 &); sleep 0.5; mkdir {ok}/sub && echo e > {ok}/sub/e",
    )
    assert (ok / "a").read_text() == "a\n" and (ok / "sub/e").read_text() == "e\n"
    assert not any(no.iterdir())
    assert result.stdout.count("Operation not permitted") == 2


async def test_a_protected_path_inside_a_writable_one_is_refused_unless_granted_again(platform, tmp_path):
    (tmp_path / "pkg/install/app").mkdir(parents=True)
    rules = [(True, tmp_path), (False, tmp_path / "pkg/install")]
    c = _confinement(platform, rules)
    await _sh(c, tmp_path, "touch pkg/x; touch pkg/install/y; rm -rf pkg/install/app")
    assert (tmp_path / "pkg/x").exists() and not (tmp_path / "pkg/install/y").exists()
    assert (tmp_path / "pkg/install/app").exists()
    regranted = _confinement(platform, [*rules, (True, tmp_path / "pkg/install")])
    await _sh(regranted, tmp_path, "touch pkg/install/y")
    assert (tmp_path / "pkg/install/y").exists()


async def test_inside_the_writable_paths_output_is_identical_to_running_unconfined(platform, tmp_path):
    """agent 无感：同一组命令关进沙箱与不关，范围内的结果逐字相同。"""
    commands = (
        "set -e; mkdir -p d/e; echo hi > d/e/f; cat d/e/f; ls -la d | awk '{print $1, $NF}'; "
        "python3 -c 'import sys,os; print(sys.version_info[0], os.getcwd() == os.path.realpath(\".\"))'; "
        "git init -q r && git -C r config user.email a@b && git -C r config user.name a && "
        "echo x > r/x && git -C r add x && git -C r commit -qm m && git -C r log --format=%s; "
        "mv d/e/f d/g; rm -r d/e; ls d; tmp=$(mktemp); echo t > $tmp; cat $tmp; rm $tmp; "
        "ps -p $$ -o pid= | wc -l | tr -d ' '; echo err >&2; exit 3"
    )
    runs = []
    for name, confinement in (
        ("open", _confinement(platform, [(True, Path("/"))])),
        (
            "confined",
            _confinement(
                platform,
                [
                    *((True, p) for c in DEFAULT_WRITABLE for p in _adapter(platform).paths(c)),
                    (True, tmp_path),
                ],
            ),
        ),
    ):
        cwd = tmp_path / name
        cwd.mkdir()
        runs.append(await _sh(confinement, cwd, commands))
    assert runs[0] == runs[1]
    assert (
        runs[0].exit_code == 3 and "hi" in runs[0].stdout and "Operation not permitted" not in runs[0].stdout
    )


async def test_report_mode_lets_writes_outside_through(platform, tmp_path):
    ok, no = tmp_path / "ok", tmp_path / "no"
    ok.mkdir(), no.mkdir()
    c = _confinement(platform, [(True, ok), (True, Path("/dev"))], mode="report")
    result = await _sh(c, ok, f"echo b > {no}/b")
    assert result.exit_code == 0 and (no / "b").read_text() == "b\n"
    assert not c.enforcing


async def test_unreadable_paths_hide_their_contents_from_commands_and_children(platform, tmp_path):
    secret, backups, other = tmp_path / "config.toml", tmp_path / "backups", tmp_path / "other"
    backups.mkdir()
    secret.write_text("key\n"), (backups / "db").write_text("db\n"), other.write_text("ok\n")
    (tmp_path / "link").symlink_to(secret)
    c = Confinement(
        sandbox_for("enforce", Path.home(), platform),
        WritablePaths.of([(True, tmp_path), (True, Path("/dev"))], "agent-x"),
        Path.home(),
        lambda p: "",
        (*real_forms(secret), *real_forms(backups)),
    )
    result = await _sh(
        c,
        tmp_path,
        f"cat {secret}; cat link; python3 -c \"open('{backups}/db').read()\"; ls {backups}; "
        f"(nohup sh -c 'cat {secret} > leak' >/dev/null 2>&1 &); sleep 0.5; "
        f"test -f {secret} && echo exists; cat {other}",
    )
    assert "key" not in result.stdout and "db\n" not in result.stdout
    assert result.stdout.count("Operation not permitted") == 4
    assert result.stdout.endswith("exists\nok\n")
    assert (tmp_path / "leak").read_text() == ""
    assert not c.readable(secret) and not c.readable(tmp_path / "link") and not c.readable(backups / "db")
    assert c.readable(other)


def _adapter(platform: str) -> Sandbox:
    return sandbox_for("enforce", Path.home(), platform)


async def test_each_category_maps_to_paths_and_temp_covers_the_temp_dir(platform):
    sandbox = _adapter(platform)
    for category in Category:
        assert sandbox.paths(category), category
    temp = WritablePaths.of([(True, p) for p in sandbox.paths(Category.TEMP)], "x")
    import tempfile

    assert temp.permits(Path(tempfile.gettempdir()) / "f") and temp.permits(Path("/tmp/f"))
    # 平台自身的安装就是运行它的 Python 环境
    assert Path(sys.prefix) in sandbox.paths(Category.PLATFORM_INSTALL)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
async def test_macos_programs_that_cannot_run_sandboxed_still_run(tmp_path):
    """setuid 程序（ps）、自己建沙箱的（sandbox-exec 本身、Chromium）在 Seatbelt 里跑不起来，放到沙箱外执行。"""
    c = _confinement("darwin", [(True, tmp_path), (True, Path("/dev"))])
    result = await _sh(
        c, tmp_path, "ps -p $$ -o pid= >/dev/null && sandbox-exec -p '(version 1)(allow default)' echo nested"
    )
    assert (result.exit_code, result.stdout) == (0, "nested\n")
    shells = sorted(
        Path.home().glob("Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")
    )
    if shells:
        result = await _sh(
            c, tmp_path, f"'{shells[-1]}' --headless --dump-dom 'data:text/html,<p>hi</p>' 2>/dev/null"
        )
        assert "<p>hi</p>" in result.stdout


def test_a_system_without_an_adapter_is_refused_not_left_unconfined():
    with pytest.raises(RuntimeError, match="no write sandbox"):
        sandbox_for("enforce", Path.home(), "plan9")


# ---- 领域：授权规则、可写路径的组成、文件工具的检查、被拒说明 ----


def test_writable_path_rules_are_literal_directories_written_with_write():
    from muliao.agents.permissions import check_patterns

    check_patterns(
        ["write:path:~/projects/arb", "write:path:/opt/data", "write:path:~/Library/Mobile Documents/x"]
    )
    for bad in ("use:path:~/x", "*:path:~/x", "write:path:relative/x", "write:path:~/x/*", "write:path:~"):
        with pytest.raises(InvalidInput, match="writable path rule"):
            check_patterns([bad])
    rules = Rules(allow=("write:path:~/a", "use:skill:s", "write:*"), deny=("write:path:~/a/b",))
    assert rules.paths() == (("~/a",), ("~/a/b",))


async def test_an_agent_writes_its_workspace_the_defaults_and_its_grants_minus_protected_and_denied(tmp_path):
    # 不在临时目录下（临时目录默认可写）；只用来展开授权里的 ~/，不写任何东西
    home = Path("/nonexistent-home-416")
    sandbox = sandbox_for("enforce", home)
    tree = tmp_path / "assets"

    async def scope_of(agent: str) -> WriteScope:
        return WriteScope(
            tree, (tree / "a1",), ("~/proj",), ("~/proj/secret",), agent, load_tree(tree, {}, 4000, None)
        )

    confiner = Confiner(sandbox, lambda a: tmp_path / "ws" / a, scope_of, home=home, private=())
    w = (await confiner.of("a1")).writable
    assert w.owner == "a1"
    assert w.permits(tmp_path / "ws/a1/x")
    assert w.permits(home / "proj/x") and not w.permits(home / "proj/secret/x")
    assert not w.permits(home / "Documents/x")
    assert not w.permits(Path(sys.prefix) / "lib/x")
    assert w.permits(home / ".cache/uv/x") and w.permits(home / "Library/Caches/x")
    # 资源树整个按归属管，哪怕它落在默认可写的临时目录里
    assert w.permits(tree / "a1/memory.md") and not w.permits(tree / "a2/memory.md")
    assert not w.permits(tree / "platform/access.toml")


# ---- 资源树按归属（#499） ----


async def _tree_fixture(container, make_agent):
    """alice 有一个 skill；平台有共享记忆 trading，角色 trader 能改它；bob 带 trader，carol 什么都没有，root 是 admin。"""
    from muliao.agents.api import RoleSpec

    alice = await make_agent("alice", model="tools")
    await container.skills.write(
        "alice-skill", "alice's skill", "body", version=None, actor="user", owner=alice.id
    )
    await container.memory.put("trading", "shared notes", version=None, actor="user", owner=None)
    await container.distribution.create(RoleSpec("trader", "trades", ["*:memory:trading"], []), actor="user")
    bob = await make_agent("bob", model="tools", roles=("trader",))
    carol = await make_agent("carol", model="tools")
    root = await make_agent("root", model="tools", roles=("admin",))
    return alice, bob, carol, root, container.settings.assets_dir


async def test_asset_tree_is_writable_by_ownership_roles_and_admin(container, make_agent):
    alice, bob, carol, root, tree = await _tree_fixture(container, make_agent)
    skill, shared = tree / "alice/skills/alice-skill", tree / "platform/memory/trading.md"
    access = tree / "platform/access.toml"

    async def can(agent_id: str, path: Path) -> bool:
        return (await container.confiner.of(agent_id)).writable.permits(path)

    # owner：自己的目录整个能写（含新建 skill）
    assert await can(alice.id, skill / "SKILL.md") and await can(alice.id, tree / "alice/skills/new/SKILL.md")
    assert await can(alice.id, tree / "alice/memory.md") and await can(alice.id, tree / "alice/agent.toml")
    # 非 owner 改不了别人的
    assert not await can(carol.id, skill / "SKILL.md") and not await can(carol.id, tree / "alice/memory.md")
    assert not await can(carol.id, shared)
    # 被授权 write 的角色成员能改共享记忆，平台别的文件照样不行
    assert await can(bob.id, shared)
    assert not await can(bob.id, tree / "platform/memory/global.md")
    assert not await can(bob.id, tree / "platform/skills/x/SKILL.md")
    # agent 改不了自己的权限：access.toml 只有 admin 能写
    for a in (alice, bob, carol):
        assert not await can(a.id, access)
    # 资源树只按归属：祖先目录的 write:path 授权也放不开它
    await container.agents.update(carol.id, AgentPatch(allow=[f"write:path:{tree.parent}"]), actor="user")
    assert await can(carol.id, tree.parent / "elsewhere")
    assert not await can(carol.id, access) and not await can(carol.id, skill / "SKILL.md")
    assert await can(carol.id, tree / "carol/memory.md")
    # admin 整棵树
    for p in (
        access,
        skill / "SKILL.md",
        shared,
        tree / "carol/memory.md",
        tree / "platform/skills/x/SKILL.md",
    ):
        assert await can(root.id, p)


async def test_refused_asset_writes_say_whose_it_is_and_whom_to_ask(container, make_agent, models):
    alice, bob, carol, _root, tree = await _tree_fixture(container, make_agent)
    skill_md = tree / "alice/skills/alice-skill/SKILL.md"
    before = skill_md.read_text()
    outputs = await run_tools(
        container,
        models,
        carol.id,
        [
            ("run_shell", {"command": f"echo x >> {skill_md}", "timeout": 30}),
            ("write_file", {"path": str(tree / "platform/access.toml"), "content": "[agents.carol]\n"}),
            (
                "write_file",
                {
                    "path": str(tree / "carol/skills/mine/SKILL.md"),
                    "content": "---\ndescription: mine\n---\nx\n",
                },
            ),
        ],
    )
    assert "Operation not permitted" in outputs[0]
    assert "belongs to agent 'alice' (skill 'alice-skill')" in outputs[0]
    assert "'write:skill:alice-skill'" in outputs[0]
    assert "only by an admin" in outputs[1]
    assert outputs[2].startswith("created ")
    assert skill_md.read_text() == before
    # 角色成员用文件工具改共享记忆
    outputs = await run_tools(
        container,
        models,
        bob.id,
        [
            (
                "edit_file",
                {"path": str(tree / "platform/memory/trading.md"), "old_text": "shared", "new_text": "joint"},
            )
        ],
    )
    assert "joint notes" in (tree / "platform/memory/trading.md").read_text()
    # 只给了单个文件：在同目录建临时文件再改名的 sed -i 被拒（文档写明的行为）
    shared = tree / "platform/memory/trading.md"
    outputs = await run_tools(
        container,
        models,
        bob.id,
        [("run_shell", {"command": f"sed -i '' s/joint/x/ {shared}", "timeout": 30})],
    )
    assert "Operation not permitted" in outputs[0] and "joint notes" in shared.read_text()


async def test_a_stdio_mcp_server_runs_inside_its_users_write_sandbox(container, make_agent, tmp_path):
    from muliao.agents.api import USER, McpServer, McpTransport, mcp_config

    me = await make_agent("alpha", model="tools")
    outside = tmp_path / "outside"
    outside.mkdir()
    inside = container.agents.workspace(me.id) / "from-mcp"
    script = tmp_path / "server.py"
    script.write_text(
        "import sys\n"
        "for p in sys.argv[1:]:\n"
        "    try:\n"
        "        open(p, 'w').write('x')\n"
        "    except OSError:\n"
        "        pass\n"
        "from mcp.server.mcpserver import MCPServer\n"
        "m = MCPServer('t')\n"
        "@m.tool()\n"
        "def ping() -> str:\n"
        "    return 'pong'\n"
        "m.run()\n"
    )
    # 临时目录默认可写：把 outside 收回来
    await container.agents.update(me.id, AgentPatch(deny=[f"write:path:{outside}"]), actor="user")
    config = mcp_config(
        transport=McpTransport.STDIO,
        command=sys.executable,
        args=[str(script), str(outside / "f"), str(inside)],
    )
    server = McpServer(owner=me.id, name="probe", config=config, enabled=True)
    assert await container.mcp.probe(server, me.id) == ["ping"]
    assert inside.read_text() == "x"
    assert not (outside / "f").exists()
    # 用户在界面上测试：关在 owner 的限制里
    inside.unlink()
    assert await container.mcp.probe(server, USER) == ["ping"]
    assert inside.read_text() == "x" and not (outside / "f").exists()
    # 平台的（只有 admin 能写配置）由用户测试时不关
    platform = McpServer(owner=None, name="probe", config=config, enabled=True)
    assert await container.mcp.probe(platform, USER) == ["ping"]
    assert (outside / "f").read_text() == "x"


def test_file_tools_are_refused_with_the_system_error_and_the_note(tmp_path):
    c = _confinement("darwin", [(True, tmp_path / "ok")])
    assert reach(c, tmp_path, "ok/f", "write") == tmp_path / "ok/f"
    with pytest.raises(Forbidden, match=r"Operation not permitted: '.*/no/f'\n\[.*write:path"):
        reach(c, tmp_path, "no/f", "write")
    assert reach(c, tmp_path, "no/f", "read") == tmp_path / "no/f"
    report = _confinement("darwin", [(True, tmp_path / "ok")], mode="report")
    assert reach(report, tmp_path, str(tmp_path / "no/f"), "write") == tmp_path / "no/f"


def test_the_note_is_added_only_when_the_refused_path_is_outside(tmp_path):
    c = _confinement("darwin", [(True, tmp_path / "ok")])
    assert denial_note(c, f"touch: {tmp_path}/no/f: Operation not permitted") is not None
    assert (
        denial_note(c, f"PermissionError: [Errno 1] Operation not permitted: '{tmp_path}/no/f'") is not None
    )
    assert denial_note(c, f"Error: EPERM: operation not permitted, open '{tmp_path}/no/f'") is not None
    # 范围内的拒绝（文件只读等）、EACCES 都不是写沙箱拦的
    assert denial_note(c, f"bash: {tmp_path}/ok/f: Operation not permitted") is None
    assert denial_note(c, f"bash: {tmp_path}/no/f: Permission denied") is None
    assert denial_note(c, "all good") is None
    assert denial_note(c, "rm: f: Operation not permitted") is None
    report = _confinement("darwin", [(True, tmp_path / "ok")], mode="report")
    assert denial_note(report, f"touch: {tmp_path}/no/f: Operation not permitted") is None


# ---- 端到端：agent 看到的 ----


async def test_run_shell_and_write_file_refuse_a_denied_path_and_leave_the_rest_unchanged(
    container, make_agent, models, tmp_path
):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    me = await make_agent("worker", model="tools")
    await container.agents.update(me.id, AgentPatch(deny=[f"write:path:{blocked}"]), actor="user")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("run_shell", {"command": f"echo in > in.txt && cat in.txt; touch {blocked}/x", "timeout": 30}),
            ("write_file", {"path": f"{blocked}/y", "content": "y"}),
            ("write_file", {"path": f"{tmp_path}/fine", "content": "z"}),
        ],
    )
    assert outputs[0].startswith("exit_code: 1\nin\n")
    assert (
        f"touch: {blocked}/x: Operation not permitted\n[If this was a write: {blocked}/x is outside"
        in outputs[0]
    )
    assert f"[Errno 1] Operation not permitted: '{blocked}/y'" in outputs[1]
    assert outputs[2].startswith("created ")
    assert not any(blocked.iterdir())


async def test_a_precheck_refused_a_write_gets_the_note_after_its_error(container, make_agent, tmp_path):
    from muliao.triggers.precheck import Precheck, PrecheckFailed, PrecheckRunner

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    me = await make_agent("watcher")
    await container.agents.update(me.id, AgentPatch(deny=[f"write:path:{blocked}"]), actor="user")
    runner = PrecheckRunner(container.vault, container.agents.workspace, container.confiner.of, 30, 1000)
    with pytest.raises(PrecheckFailed, match=r"Operation not permitted\n\[.*is outside"):
        await runner.run(me.id, Precheck(command=f"echo x > {blocked}/state"), None)
    assert await runner.run(me.id, Precheck(command="echo x > state && cat state"), None) == "x\n"


# ---- 平台的私有文件只有 admin 能读写（#539） ----


@pytest.fixture
def settings(settings):
    """配置文件在 data 目录旁边（同 load_settings 读入的），它与备份目录、推送私钥是平台的私有文件。"""
    settings._file = settings.data_dir.parent / "config.toml"
    return settings


async def test_private_files_are_readable_and_writable_only_by_an_admin(container, make_agent, models):
    config = container.settings.data_dir.parent / "config.toml"
    backups = config.parent / "backups"
    backups.mkdir()
    config.write_text("password = 'hunter22'\n")
    (backups / "config.toml.1").write_text("password = 'hunter22'\n")
    assert container.settings.private_files == (
        config,
        config.with_name("config.toml.prev"),
        backups,
        container.settings.vapid_key_path,
    )
    carol = await make_agent("carol", model="tools")
    root = await make_agent("root", model="tools", roles=("admin",))
    # 祖先目录的 write:path 授权也放不开
    await container.agents.update(carol.id, AgentPatch(allow=[f"write:path:{config.parent}"]), actor="user")
    calls = [
        (
            "run_shell",
            {"command": f"cat {config}; cat {backups}/config.toml.1; echo x >> {config}", "timeout": 30},
        ),
        ("read_file", {"path": str(config)}),
        ("read_file", {"reads": [{"path": str(backups / "config.toml.1")}, {"path": "/dev/null"}]}),
        ("edit_file", {"path": str(config), "old_text": "hunter22", "new_text": "x"}),
    ]
    outputs = await run_tools(container, models, carol.id, calls)
    assert not any("hunter22" in o for o in outputs)
    assert outputs[0].count("Operation not permitted") == 3
    assert outputs[0].count("one of the platform's private files") == 1
    for o in outputs[1:]:
        assert "Operation not permitted" in o and "only an agent with the admin role" in o
    assert config.read_text() == "password = 'hunter22'\n"
    # 别的地方照常
    assert (await container.confiner.of(carol.id)).writable.permits(config.parent / "elsewhere")
    outputs = await run_tools(
        container,
        models,
        root.id,
        [("run_shell", {"command": f"cat {config} {backups}/config.toml.1", "timeout": 30}), calls[1]],
    )
    assert outputs[0].count("hunter22") == 2 and "hunter22" in outputs[1]


async def test_report_mode_lets_reads_of_private_files_through(platform, tmp_path):
    secret = tmp_path / "config.toml"
    secret.write_text("key\n")
    c = Confinement(
        sandbox_for("report", Path.home(), platform),
        WritablePaths.of([(True, tmp_path), (True, Path("/dev"))], "agent-x"),
        Path.home(),
        lambda p: "",
        real_forms(secret),
    )
    result = await _sh(c, tmp_path, f"cat {secret}")
    assert (result.exit_code, result.stdout) == (0, "key\n")
    assert reach(c, tmp_path, str(secret), "read") == secret


async def test_a_precheck_cannot_read_a_private_file(container, make_agent):
    from muliao.triggers.precheck import Precheck, PrecheckFailed, PrecheckRunner

    config = container.settings.data_dir.parent / "config.toml"
    config.write_text("password = 'hunter22'\n")
    me = await make_agent("watcher")
    runner = PrecheckRunner(container.vault, container.agents.workspace, container.confiner.of, 30, 1000)
    with pytest.raises(PrecheckFailed, match=r"Operation not permitted\n\[.*private files"):
        await runner.run(me.id, Precheck(command=f"cat {config}"), None)


async def test_private_files_stay_private_under_another_spelling_and_through_share_file(
    container, make_agent, models
):
    """#570：大小写不同的写法、share_file 都按同一个入口（writable.reach）判断，读不到私有文件。"""
    config = container.settings.data_dir.parent / "config.toml"
    config.write_text("password = 'hunter22'\n")
    if not (config.parent / "CONFIG.TOML").exists():
        pytest.skip("the file system here is case-sensitive")
    carol = await make_agent("carol", model="tools")
    shouted = str(config.parent / "CONFIG.TOML")
    outputs = await run_tools(
        container,
        models,
        carol.id,
        [
            ("read_file", {"path": shouted}),
            ("share_file", {"path": shouted}),
            ("share_file", {"path": str(config)}),
            ("write_file", {"path": shouted, "content": "x"}),
        ],
    )
    for o in outputs:
        assert "Operation not permitted" in o and "only an agent with the admin role" in o
    assert config.read_text() == "password = 'hunter22'\n"


def test_writes_are_judged_by_the_files_identity_whatever_the_spelling(tmp_path):
    (tmp_path / "Install").mkdir()
    c = _confinement("darwin", [(True, tmp_path), (False, tmp_path / "Install")])
    if not (tmp_path / "INSTALL").exists():
        pytest.skip("the file system here is case-sensitive")
    for spelling in ("Install/x", "INSTALL/x", "install/new/y"):
        assert not c.writable.permits(tmp_path / spelling)
    assert c.writable.permits(tmp_path / "other")
