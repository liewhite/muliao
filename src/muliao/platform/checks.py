"""运行中检查（心跳）的节拍：循环每 TICK_SECONDS 让每种并发工作（后台任务、委派）看一遍哪些到期了。间隔与到期的
规则、投信都在会话领域（sessions.work 的 DEFAULT_CHECK_SECONDS / check_due，sessions.mailbox.post_check_in），这里
只管节拍。设计见 docs/architecture.md「占用与结清」。
"""

import logging
from collections.abc import Sequence
from typing import Protocol

import anyio

log = logging.getLogger(__name__)

# 循环看一遍到期工作的间隔：决定心跳与超时的精度
TICK_SECONDS = 1.0


class PeriodicCheck(Protocol):
    async def check(self) -> int:
        """给到期的工作投检查信件（并执行到期的超时），返回投出的封数。"""
        ...


class WorkWatcher:
    """运行中检查的循环：每种工作按各自的间隔，循环只管节拍。"""

    def __init__(self, works: Sequence[PeriodicCheck], tick_seconds: float = TICK_SECONDS):
        self._works = works
        self._tick = tick_seconds

    async def check(self) -> int:
        return sum([await work.check() for work in self._works])

    async def run_forever(self) -> None:
        while True:
            await anyio.sleep(self._tick)
            posted = await self.check()
            if posted:
                log.info("check-in events posted", extra={"posted": posted})
