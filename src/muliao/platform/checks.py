"""运行中检查（心跳）的节奏：在会话之外并发进行的工作（后台任务、delegate_task(report_back) 的委派）各按自己的
间隔（发起时的 heartbeat_seconds，存在工作自己的行上）向发起会话投检查信件；阻塞等待的委派（ask_agent）的超时也在
这个循环里执行。循环每 TICK_SECONDS 让每种工作看一遍哪些到期了；投信的规则在会话领域（sessions.mailbox.post_check_in）。
设计见 docs/architecture.md「占用与结清」。
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

import anyio

log = logging.getLogger(__name__)

# 发起并发工作时不给 heartbeat_seconds 的默认间隔（run_shell(background)、delegate_task(report_back) 共用，唯一一处）
DEFAULT_CHECK_SECONDS = 30.0

# 循环看一遍到期工作的间隔：决定心跳与超时的精度
TICK_SECONDS = 1.0


def check_due(last: datetime, interval_seconds: float, now: datetime) -> bool:
    """上一次检查（还没检查过时是工作开始的时刻）之后过了一个间隔。"""
    return now - last >= timedelta(seconds=interval_seconds)


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
