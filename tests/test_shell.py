"""run_bash：run_shell 工具与前置检查（经 AgentShell.run）共用的命令执行。"""

from pathlib import Path

from muliao.platform.sandbox import Confinement, WritablePaths, sandbox_for
from muliao.platform.shell import run_bash

# 不限写的路径：这里测的是命令执行本身，写沙箱在 test_sandbox.py
OPEN = Confinement(
    sandbox_for("enforce", Path.home()),
    WritablePaths.of([(True, Path("/"))], "test"),
    Path.home(),
    lambda p: f"[{p} refused]",
)


async def test_a_timed_out_command_keeps_the_output_it_produced_before_being_killed(tmp_path):
    """超时被杀时，之前的输出是判断卡在哪里的唯一线索，不能丢。"""
    result = await run_bash(
        "echo progress-1; seq 1 20000 >/dev/stderr; echo progress-2; sleep 5",
        cwd=tmp_path,
        confinement=OPEN,
        secrets={},
        timeout=1,
    )
    assert result.exit_code is None
    assert result.stdout.split() == ["progress-1", "progress-2"]
    assert result.stderr.split()[-1] == "20000"


async def test_a_command_that_ignores_stdin_is_not_a_failure(tmp_path):
    result = await run_bash(
        "exit 0", cwd=tmp_path, confinement=OPEN, secrets={}, timeout=5, stdin=b"x" * 1_000_000
    )
    assert result.exit_code == 0


async def test_large_input_and_output_do_not_deadlock(tmp_path):
    result = await run_bash(
        "cat; cat >/dev/null", cwd=tmp_path, confinement=OPEN, secrets={}, timeout=10, stdin=b"y\n" * 500_000
    )
    assert result.exit_code == 0 and len(result.stdout) == 1_000_000


async def test_secrets_are_redacted_from_both_streams(tmp_path):
    result = await run_bash(
        'echo "$TOKEN"; echo "$TOKEN" >&2',
        cwd=tmp_path,
        confinement=OPEN,
        secrets={"TOKEN": "s3cr3t"},
        timeout=5,
    )
    assert result.stdout.strip() == result.stderr.strip() == "[secret:TOKEN]"


async def test_a_command_ends_when_bash_exits_even_if_a_background_child_keeps_the_output_open(
    tmp_path, monkeypatch
):
    """#337：前台以进程退出为准（同后台任务），不等留在后台的子进程关闭管道、一直挂到 timeout。"""
    import time

    import muliao.platform.shell as shell_module

    monkeypatch.setattr(shell_module, "DRAIN_SECONDS", 0.3)
    started = time.monotonic()
    result = await run_bash(
        "echo main; sleep 10 & exit 3", cwd=tmp_path, confinement=OPEN, secrets={}, timeout=20
    )
    assert time.monotonic() - started < 5
    assert (result.exit_code, result.stdout, result.left_running) == (3, "main\n", True)


async def test_reading_after_the_exit_does_not_count_against_the_timeout(tmp_path, monkeypatch):
    """退出在时限前、补读跨过时限：仍是正常退出，后代不被当成超时杀掉。"""
    import muliao.platform.shell as shell_module

    monkeypatch.setattr(shell_module, "DRAIN_SECONDS", 1.5)
    result = await run_bash(
        "echo main; sleep 0.5; sleep 10 & exit 3", cwd=tmp_path, confinement=OPEN, secrets={}, timeout=1.2
    )
    assert (result.exit_code, result.stdout, result.left_running) == (3, "main\n", True)


async def test_a_command_that_closes_its_output_is_fully_read(tmp_path):
    result = await run_bash(
        "sleep 10 >/dev/null 2>&1 & echo done", cwd=tmp_path, confinement=OPEN, secrets={}, timeout=20
    )
    assert (result.exit_code, result.stdout, result.left_running) == (0, "done\n", False)


def test_a_secret_containing_another_secret_is_redacted_whole():
    from muliao.platform.shell import redact

    # 短的那个先登记：逐个替换时会先换掉它，长的那个的其余部分漏出来
    secrets = {"SHORT": "abc", "LONG": "abc-tail-xyz"}
    assert redact("x abc-tail-xyz y abc", secrets) == "x [secret:LONG] y [secret:SHORT]"
    # 替换出的 [secret:KEY] 不会再被别的 secret 匹配
    assert redact("p", {"A": "p", "B": "secret"}) == "[secret:A]"
