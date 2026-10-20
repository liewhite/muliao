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
# 进程结束（或被杀）后读完管道里剩余输出的时限：脱离进程组的后代可能一直持有管道
DRAIN_SECONDS = 5.0


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


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # 进程恰好已经退出


async def _collect(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
    assert stream is not None
    while chunk := await stream.read(65536):
        buf += chunk


async def spawn_bash(
    command: str,
    *,
    cwd: Path,
    secrets: Mapping[str, str],
    stdin: bool = False,
    merge_stderr: bool = False,
    pipefail: bool = False,
) -> asyncio.subprocess.Process:
    """在 cwd 里启动 bash -c command：只继承少量环境变量、注入 secret、自成进程组（kill_group 能杀掉它的整棵进程树）。
    run_bash（等它结束）与后台任务（runtime.jobs）共用。"""
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ} | dict(secrets)
    await asyncio.to_thread(cwd.mkdir, parents=True, exist_ok=True)
    return await asyncio.create_subprocess_exec(
        "bash",
        *(["-o", "pipefail"] if pipefail else []),
        "-c",
        command,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        start_new_session=True,
    )


async def run_bash(
    command: str,
    *,
    cwd: Path,
    secrets: Mapping[str, str],
    timeout: float,  # noqa: ASYNC109  调用方给出的时限，超时要杀掉进程组而不只是取消等待
    stdin: bytes | None = None,
    merge_stderr: bool = False,
    pipefail: bool = False,
) -> ShellResult:
    """pipefail：管道里任何一步失败都算失败（bash 默认只看最后一步），用于靠退出码判断成败的调用方。"""
    proc = await spawn_bash(
        command,
        cwd=cwd,
        secrets=secrets,
        stdin=stdin is not None,
        merge_stderr=merge_stderr,
        pipefail=pipefail,
    )
    # 输出持续累积到这里：超时取消读取时，已经读到的部分不能随之丢掉（communicate 的局部缓冲会丢）
    out, err = bytearray(), bytearray()
    streams = [(proc.stdout, out), *([(proc.stderr, err)] if proc.stderr is not None else [])]

    async def feed() -> None:
        if stdin is None:
            return
        assert proc.stdin is not None
        try:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            # 命令不读 stdin 就退出了：这是正常的（与 communicate 的处理一致），结果看它的退出码与输出
            pass

    async def run() -> None:
        # 写入与读取并发：输入大、命令又先输出很多时，顺序进行会让两边的管道都写满而互相等待。
        # TaskGroup：其中一个意外失败时其余的一起取消，不留在后台
        async with asyncio.TaskGroup() as tg:
            tg.create_task(feed())
            for stream, buf in streams:
                tg.create_task(_collect(stream, buf))
            tg.create_task(proc.wait())

    exit_code: int | None
    try:
        await asyncio.wait_for(run(), timeout=timeout)
        exit_code = proc.returncode
    except TimeoutError:
        kill_group(proc.pid)
        # 把杀掉之前写进管道、还没读的部分读完；脱离进程组的后代可能仍持有管道，所以收尾也有时限
        drain = [asyncio.create_task(_collect(stream, buf)) for stream, buf in streams]
        _, unfinished = await asyncio.wait(drain, timeout=DRAIN_SECONDS)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)
        exit_code = None
    except BaseException:
        # 被取消（关停、停止一轮）或读写意外失败：不留下还在跑的命令
        kill_group(proc.pid)
        raise
    return ShellResult(
        exit_code=exit_code,
        stdout=redact(out.decode(errors="replace"), secrets),
        stderr=redact(err.decode(errors="replace"), secrets),
    )
