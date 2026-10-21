"""run_bash：run_shell 工具与前置检查共用的命令执行。"""

from muliao.platform.shell import run_bash


async def test_a_timed_out_command_keeps_the_output_it_produced_before_being_killed(tmp_path):
    """超时被杀时，之前的输出是判断卡在哪里的唯一线索，不能丢。"""
    result = await run_bash(
        "echo progress-1; seq 1 20000 >/dev/stderr; echo progress-2; sleep 5",
        cwd=tmp_path,
        secrets={},
        timeout=1,
    )
    assert result.exit_code is None
    assert result.stdout.split() == ["progress-1", "progress-2"]
    assert result.stderr.split()[-1] == "20000"


async def test_a_command_that_ignores_stdin_is_not_a_failure(tmp_path):
    result = await run_bash("exit 0", cwd=tmp_path, secrets={}, timeout=5, stdin=b"x" * 1_000_000)
    assert result.exit_code == 0


async def test_large_input_and_output_do_not_deadlock(tmp_path):
    result = await run_bash(
        "cat; cat >/dev/null", cwd=tmp_path, secrets={}, timeout=10, stdin=b"y\n" * 500_000
    )
    assert result.exit_code == 0 and len(result.stdout) == 1_000_000


async def test_secrets_are_redacted_from_both_streams(tmp_path):
    result = await run_bash(
        'echo "$TOKEN"; echo "$TOKEN" >&2', cwd=tmp_path, secrets={"TOKEN": "s3cr3t"}, timeout=5
    )
    assert result.stdout.strip() == result.stderr.strip() == "[secret:TOKEN]"


def test_a_secret_containing_another_secret_is_redacted_whole():
    from muliao.platform.shell import redact

    # 短的那个先登记：逐个替换时会先换掉它，长的那个的其余部分漏出来
    secrets = {"SHORT": "abc", "LONG": "abc-tail-xyz"}
    assert redact("x abc-tail-xyz y abc", secrets) == "x [secret:LONG] y [secret:SHORT]"
    # 替换出的 [secret:KEY] 不会再被别的 secret 匹配
    assert redact("p", {"A": "p", "B": "secret"}) == "[secret:A]"
