"""在 agent 工作目录里执行一条 bash 命令：run_shell 工具与触发器的前置检查共用这一份实现。

只继承少量环境变量，按 key 注入 secret，输出里的 secret 明文替换为 [secret:KEY]；超时杀掉整个进程组。
不做强隔离（单人本机使用，见架构文档「安全边界」）。
"""

import asyncio
import os
import re
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import anyio

# 只继承这些环境变量，不把服务进程的其余环境带给命令
_INHERITED_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL")
# 进程结束（或被杀）后读完管道里剩余输出的时限：脱离进程组的后代可能一直持有管道
DRAIN_SECONDS = 5.0
_EXIT_POLL_SECONDS = 0.2
# 进程已退出、它留在后台的后代还占着输出管道时附在结果里的说明（前台与后台任务同一句）
LEFT_RUNNING_NOTE = (
    "Processes it left running in the background still hold its output; their later output is not recorded."
)


@dataclass(frozen=True)
class ShellResult:
    # 超时被杀时为 None
    exit_code: int | None
    stdout: str
    # merge_stderr 时 stderr 已并入 stdout，这里为空
    stderr: str
    # 进程退出了，但它留在后台的后代还占着输出管道：之后的输出没有读
    left_running: bool = False


def redact(text: str, secrets: Mapping[str, str]) -> str:
    """一遍扫描替换所有 secret，同一位置优先匹配最长的：逐个 replace 时，A 是 B 的子串而 A 先替换，B 的其余部分
    就会漏出来；先替换出的 [secret:KEY] 也可能被后面的 secret 再次匹配。"""
    if not secrets:
        return text
    key_of = {value: key for key, value in secrets.items()}
    pattern = "|".join(re.escape(v) for v in sorted(key_of, key=len, reverse=True))
    return re.sub(pattern, lambda m: f"[secret:{key_of[m.group()]}]", text)


def kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # 进程恰好已经退出
    except PermissionError:
        # macOS：组里只剩已退出、还没被回收的进程时 killpg 返回 EPERM（还有活着的成员时信号照常送达）。组属于我们
        # 自己起的进程，不会是权限问题；pid 已被别人复用的情形不会走到这里（调用方只对自己还持有的进程调用）
        pass


async def _collect(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
    assert stream is not None
    while chunk := await stream.read(65536):
        buf += chunk


async def wait_exit_code(proc: asyncio.subprocess.Process) -> int:
    """进程退出时的退出码。不用 Process.wait()：在进程退出前开始等的话，它要等所有管道都关闭才返回（CPython
    base_subprocess 只在管道全部断开后唤醒等待者），后代一直占着管道就一直等。returncode 在进程退出时就已设置。"""
    # 轮询：退出的通知（协议的 process_exited）只在 asyncio 内部对象上，不为它去碰私有属性
    while proc.returncode is None:  # noqa: ASYNC110
        await asyncio.sleep(_EXIT_POLL_SECONDS)
    return proc.returncode


async def read_until_exit(
    proc: asyncio.subprocess.Process, readers: Sequence[Callable[[], Awaitable[None]]]
) -> tuple[int, bool]:
    """以进程退出为准（不是输出管道关闭）：并发跑 readers 读它的输出，进程退出后最多再给它们 DRAIN_SECONDS
    读完，读不完的取消（它留在后台的后代可能一直占着管道）。返回退出码与输出是否读完。前台命令与后台任务共用。
    readers 的异常照常抛出。"""
    all_read = anyio.Event()
    remaining = len(readers)
    if not remaining:
        all_read.set()

    async def read(reader: Callable[[], Awaitable[None]]) -> None:
        nonlocal remaining
        await reader()
        remaining -= 1
        if not remaining:
            all_read.set()

    async with anyio.create_task_group() as group:
        for reader in readers:
            group.start_soon(read, reader)
        async with anyio.create_task_group() as race:

            async def exited() -> None:
                await wait_exit_code(proc)
                race.cancel_scope.cancel()

            async def closed() -> None:
                # 常见情形：退出时管道随之关闭，Process.wait 立即返回，不用等轮询的间隔
                await all_read.wait()
                await proc.wait()
                race.cancel_scope.cancel()

            race.start_soon(exited)
            race.start_soon(closed)
        with anyio.move_on_after(DRAIN_SECONDS):
            await all_read.wait()
        group.cancel_scope.cancel()
    assert proc.returncode is not None
    return proc.returncode, all_read.is_set()


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
    run_bash（等它结束）与后台任务（workspace.runner）共用。"""
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

    async def run() -> tuple[int, bool]:
        # 写入与读取并发：输入大、命令又先输出很多时，顺序进行会让两边的管道都写满而互相等待。
        # 任务组：其中一个意外失败时其余的一起取消，不留在后台；命令退出后不再写（留下的后代可能不读）
        async with anyio.create_task_group() as group:
            group.start_soon(feed)
            ended = await read_until_exit(proc, [partial(_collect, stream, buf) for stream, buf in streams])
            group.cancel_scope.cancel()
        return ended

    async def drain() -> None:
        # 把杀掉之前写进管道、还没读的部分读完；脱离进程组的后代可能仍持有管道，所以收尾也有时限
        with anyio.move_on_after(DRAIN_SECONDS):
            async with anyio.create_task_group() as group:
                for stream, buf in streams:
                    group.start_soon(_collect, stream, buf)

    exit_code: int | None
    try:
        with anyio.fail_after(timeout):
            exit_code, drained = await run()
    except TimeoutError:
        kill_group(proc.pid)
        await drain()
        exit_code, drained = None, True
    except BaseException:
        # 被取消（关停、停止一轮）或读写意外失败：不留下还在跑的命令
        kill_group(proc.pid)
        raise
    # 命令退出后留在后台的后代不杀（同后台任务）：要脱离会话长跑的东西本来就该这样起，只如实说明输出没读完
    return ShellResult(
        exit_code=exit_code,
        stdout=redact(out.decode(errors="replace"), secrets),
        stderr=redact(err.decode(errors="replace"), secrets),
        left_running=not drained,
    )
