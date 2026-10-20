"""会话拥有的异步工作：会话发起、在它之外进行、结束时把结局投回发起会话的工作——后台任务（runtime.jobs）与要交回
结局的委派（runtime.delegations）。设计见 docs/architecture.md「会话拥有的异步工作」。

这里只有运行中检查的共用规则：每隔 job_check_seconds 给每项还在进行的工作向发起会话投一封检查信件（不推送），同一
来源已有信件在信箱里排着时不再发（不堆积）——post_check_in。其余共用的部分在会话领域：工作进行期间在会话上登记
占用（runtime.holds，会话据此判断工作是否做完），会话被停止时发布 SessionStopped，各自停下自己的工作。
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.models import HoldStatus, MailKind
from muliao.runtime.holds import get_in as get_hold_in
from muliao.runtime.mailbox import has_queued_from, post_in
from muliao.runtime.tasks import TaskQueue

log = logging.getLogger(__name__)


class SessionWork(Protocol):
    async def check(self) -> int:
        """给还在进行的工作投检查信件（经 post_check_in），返回投出的封数。"""
        ...


async def post_check_in(
    s: AsyncSession, queue: TaskQueue, owner_session_id: str, holder: str, source: str, text: str
) -> bool:
    """在调用方事务里给发起会话投一封检查信件；这个来源已有信件排着时不投。工作的占用已不是 active（会话被停止，或
    会话已删除）时也不投：停止之后不唤醒会话。返回是否投了。"""
    hold = await get_hold_in(s, owner_session_id, holder)
    if hold is None or hold.status != HoldStatus.ACTIVE:
        return False
    if await has_queued_from(s, owner_session_id, source):
        return False
    await post_in(s, queue, owner_session_id, kind=MailKind.EVENT, source=source, content=text, awaited=False)
    return True


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
