"""后台任务的进程与输出（纯 IO，不碰数据库）：等进程退出、把输出逐块脱敏写进日志、读日志的最后几行。"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Mapping
from pathlib import Path

import anyio

from muliao.platform.shell import kill_group, redact

_CHUNK = 65536
_TAIL_LINES = 30
_TAIL_BYTES = 16384
_EXIT_POLL_SECONDS = 0.2


def tail(path: Path) -> str:
    """日志的最后几行（读文件末尾一段，不整个读进来）。"""
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - _TAIL_BYTES))
        data = f.read()
    lines = data.decode(errors="replace").splitlines()
    if size > _TAIL_BYTES:
        # 第一行可能被截在中间
        lines = lines[1:]
    return "\n".join(lines[-_TAIL_LINES:])


async def wait_exit_code(proc: asyncio.subprocess.Process) -> int:
    """进程退出时的退出码。不用 Process.wait()：在进程退出前开始等的话，它要等所有管道都关闭才返回（CPython
    base_subprocess 只在管道全部断开后唤醒等待者），后代一直占着管道就一直等。returncode 在进程退出时就已设置。"""
    # 轮询：退出的通知（协议的 process_exited）只在 asyncio 内部对象上，不为它去碰私有属性
    while proc.returncode is None:  # noqa: ASYNC110
        await asyncio.sleep(_EXIT_POLL_SECONDS)
    return proc.returncode


def _safe_cut(text: str, cut: int, secrets: Mapping[str, str]) -> int:
    """把切口挪到任何 secret 出现位置之前：切口两侧分开脱敏时，跨在切口上的 secret 会漏出明文。
    挪一次可能让别的 secret（或同一个 secret 的另一次出现）跨到新切口上，所以挪到不再变化为止。"""
    while True:
        moved = cut
        for value in secrets.values():
            start = text.find(value, max(0, moved - len(value) + 1))
            while start != -1 and start < moved:
                if start + len(value) > moved:
                    moved = start
                start = text.find(value, start + 1)
        if moved == cut:
            return cut
        cut = moved


def _partial_secret(text: str, secrets: Mapping[str, str]) -> int:
    """text 末尾最长的、恰好是某个 secret 开头（真前缀）的一段的长度：这一段可能和下一块拼成 secret，要留着。"""
    longest = 0
    for value in secrets.values():
        for n in range(min(len(value) - 1, len(text)), longest, -1):
            if text.endswith(value[:n]):
                longest = n
                break
    return longest


class Recorder:
    """把输出写进日志。意外失败时杀掉进程组（进程不能没人管地留着），失败交给监督者记为被中断。"""

    def __init__(self, proc: asyncio.subprocess.Process, log_file: Path, secrets: Mapping[str, str]):
        self._proc = proc
        self._log_file = log_file
        self._secrets = secrets
        self.done = anyio.Event()
        self.error: Exception | None = None

    async def run(self) -> None:
        try:
            await _record(self._proc, self._log_file, self._secrets)
        except Exception as e:
            self.error = e
            kill_group(self._proc.pid)
        finally:
            self.done.set()


async def _record(proc: asyncio.subprocess.Process, log_file: Path, secrets: Mapping[str, str]) -> None:
    """把输出逐块脱敏后追加到日志。每读一块写出到切口为止：末尾可能是某个 secret 开头的一段留着等下一块
    （_partial_secret），切口再避开跨在它上面的完整 secret（_safe_cut），两侧分开脱敏也不会漏。从切口之前开始的
    secret 要么已经完整、由 _safe_cut 处理，要么不完整、那它从开头到末尾就是一个 secret 前缀，切口不会在它之后。
    secret 可以含换行，所以不能按行切。读到结尾时写出全部剩余；读到一半被取消（进程退出后后代还占着管道）时，
    只丢下留着的那一小段。增量解码，多字节字符不会被块边界切坏。"""
    assert proc.stdout is not None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    with log_file.open("a", encoding="utf-8") as out:
        while chunk := await proc.stdout.read(_CHUNK):
            pending += decoder.decode(chunk)
            cut = _safe_cut(pending, len(pending) - _partial_secret(pending, secrets), secrets)
            out.write(redact(pending[:cut], secrets))
            pending = pending[cut:]
            out.flush()
        pending += decoder.decode(b"", final=True)
        out.write(redact(pending, secrets))
