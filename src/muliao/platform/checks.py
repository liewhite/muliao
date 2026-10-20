"""运行中检查的节奏：每隔 job_check_seconds 让每种在会话之外进行的工作（后台任务、委派）给还在进行的工作向发起会话
投一封检查信件。投信的规则在会话领域（sessions.mailbox.post_check_in）。设计见 docs/architecture.md「占用与
结清」。
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

log = logging.getLogger(__name__)


class SessionWork(Protocol):
    async def check(self) -> int:
        """给还在进行的工作投检查信件，返回投出的封数。"""
        ...


class WorkWatcher:
    """运行中检查的循环：所有种类的工作同一个节奏。"""

    def __init__(self, works: Sequence[SessionWork], interval_seconds: float):
        self._works = works
        self._interval = interval_seconds

    async def check(self) -> int:
        return sum([await work.check() for work in self._works])

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            posted = await self.check()
            if posted:
                log.info("check-in events posted", extra={"posted": posted})
