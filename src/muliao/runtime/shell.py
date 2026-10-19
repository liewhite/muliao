"""在 agent 工作目录里执行一条 bash 命令：run_shell 工具与触发器的前置检查共用这一份实现。

只继承少量环境变量，按 key 注入 secret，输出里的 secret 明文替换为 [secret:KEY]；超时杀掉整个进程组。
不做强隔离（单人本机使用，见架构文档「安全边界」）。
"""

import asyncio
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# 只继承这些环境变量，不把服务进程的其余环境带给命令
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL")
_DRAIN_SECONDS = 5.0


@dataclass(frozen=True)
class ShellResult:
    # 超时被杀时为 None
    exit_code: int | None
    stdout: str
    # merge_stderr 时 stderr 已并入 stdout，这里为空
    stderr: str


def redact(text: str, secrets: Mapping[str, str]) -> str:
    for key, value in secrets.items():
        text = text.replace(value, f"[secret:{key}]")
    return text


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # 进程恰好已经退出


async def run_bash(
    command: str,
    *,
    cwd: Path,
    secrets: Mapping[str, str],
    timeout: float,  # noqa: ASYNC109  调用方给出的时限，超时要杀掉进程组而不只是取消等待
    stdin: bytes | None = None,
    merge_stderr: bool = False,
) -> ShellResult:
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ} | dict(secrets)
    await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "bash",
        "-c",
        command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    exit_code: int | None
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        exit_code = proc.returncode
    except TimeoutError:
        _kill_group(proc.pid)
        try:
            # 脱离进程组的后代可能仍持有管道，收尾读取也要有时限
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_DRAIN_SECONDS)
        except TimeoutError:
            out, err = b"", b""
        exit_code = None
    except asyncio.CancelledError:
        _kill_group(proc.pid)
        raise
    return ShellResult(
        exit_code=exit_code,
        stdout=redact(out.decode(errors="replace"), secrets),
        stderr=redact((err or b"").decode(errors="replace"), secrets),
    )
