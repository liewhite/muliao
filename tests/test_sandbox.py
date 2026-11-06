"""写沙箱（#416）：执行层的适配器按接口测（每个系统的适配器跑同一组用例，现在只有 macOS），领域层（可写路径怎么算、文件
工具的检查、被拒说明）与端到端（run_shell、write_file 对 agent 的样子）。"""

import sys
from pathlib import Path

import pytest

from muliao.agents.api import AgentPatch, Rules
from muliao.errors import Forbidden, InvalidInput
from muliao.platform.sandbox import Category, Confinement, Sandbox, WritablePaths, sandbox_for
from muliao.platform.shell import run_bash
from muliao.workspace.writable import DEFAULT_WRITABLE, Confiner, check_write, denial_note
from tests.test_tools import run_tools

ADAPTERS = [pytest.param("darwin", marks=pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"))]


@pytest.fixture(params=ADAPTERS)
def platform(request) -> str:
    return request.param


def _confinement(platform: str, rules: list[tuple[bool, Path]], mode="enforce") -> Confinement:
    return Confinement(sandbox_for(mode, platform), WritablePaths.of(rules, "agent-x"))


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
        "echo err >&2; exit 3"
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


def _adapter(platform: str) -> Sandbox:
    return sandbox_for("enforce", platform)


async def test_each_category_maps_to_paths_and_temp_covers_the_temp_dir(platform):
    sandbox = _adapter(platform)
    for category in Category:
        assert sandbox.paths(category), category
    temp = WritablePaths.of([(True, p) for p in sandbox.paths(Category.TEMP)], "x")
    import tempfile

    assert temp.permits(Path(tempfile.gettempdir()) / "f") and temp.permits(Path("/tmp/f"))
    # 平台自身的安装就是运行它的 Python 环境
    assert Path(sys.prefix) in sandbox.paths(Category.PLATFORM_INSTALL)


def test_a_system_without_an_adapter_is_refused_not_left_unconfined():
    with pytest.raises(RuntimeError, match="no write sandbox"):
        sandbox_for("enforce", "plan9")


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
    sandbox = sandbox_for("enforce")

    async def rules_of(_agent: str) -> Rules:
        return Rules(allow=("write:path:~/proj",), deny=("write:path:~/proj/secret",))

    confiner = Confiner(sandbox, lambda a: tmp_path / "ws" / a, rules_of, home=home)
    w = (await confiner.of("a1")).writable
    assert w.owner == "a1"
    assert w.permits(tmp_path / "ws/a1/x")
    assert w.permits(home / "proj/x") and not w.permits(home / "proj/secret/x")
    assert not w.permits(home / "Documents/x")
    assert not w.permits(Path(sys.prefix) / "lib/x")
    assert w.permits(Path.home() / ".cache/uv/x")


def test_file_tools_are_refused_with_the_system_error_and_the_note(tmp_path):
    c = _confinement("darwin", [(True, tmp_path / "ok")])
    check_write(c, tmp_path / "ok/f")
    with pytest.raises(Forbidden, match=r"Operation not permitted: '.*/no/f'\n\[.*write:path"):
        check_write(c, tmp_path / "no/f")
    check_write(_confinement("darwin", [(True, tmp_path / "ok")], mode="report"), tmp_path / "no/f")


def test_the_note_is_added_only_when_the_refused_path_is_outside(tmp_path):
    c = _confinement("darwin", [(True, tmp_path / "ok")])
    assert denial_note(c, f"touch: {tmp_path}/no/f: Operation not permitted") is not None
    assert (
        denial_note(c, f"PermissionError: [Errno 1] Operation not permitted: '{tmp_path}/no/f'") is not None
    )
    # 范围内的拒绝（文件只读等）不是沙箱拦的
    assert denial_note(c, f"bash: {tmp_path}/ok/f: Permission denied") is None
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
    assert f"touch: {blocked}/x: Operation not permitted\n[{blocked}/x is outside" in outputs[0]
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
