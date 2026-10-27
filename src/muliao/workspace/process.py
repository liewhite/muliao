"""后台任务的输出记录者：把进程输出写进日志（逐块脱敏的实现在 platform.output，流式触发器共用）。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import anyio

from muliao.platform.output import record_output
from muliao.platform.shell import kill_group


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
            assert self._proc.stdout is not None
            await record_output(self._proc.stdout, self._log_file, self._secrets)
        except Exception as e:
            self.error = e
            kill_group(self._proc.pid)
        finally:
            self.done.set()
