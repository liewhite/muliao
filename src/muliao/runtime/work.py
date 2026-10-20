"""会话拥有的异步工作：会话发起、在它之外进行、结束时把结局投回发起会话的工作——后台任务（runtime.jobs）与要交回
结局的委派（runtime.delegations）。设计见 docs/architecture.md「会话拥有的异步工作」。

它们共用的规则只写在这里：
- 运行中检查：每隔 job_check_seconds 给每项还在进行的工作向发起会话投一封检查信件（不推送），同一来源已有信件在
  信箱里排着时不再发（不堆积）——post_check_in。
- 随会话停止：会话被用户停止时，它发起的工作一起停下，之后不再给它投信——SessionWork.halt。
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.models import MailKind
from muliao.runtime.mailbox import has_queued_from, post_in
from muliao.runtime.tasks import TaskQueue

log = logging.getLogger(__name__)


class SessionWork(Protocol):
    async def check(self) -> int:
        """给还在进行的工作投检查信件（经 post_check_in），返回投出的封数。"""
        ...

    async def halt(self, owner_session_id: str) -> None:
        """会话被用户停止：结束它发起、还在进行的这种工作，之后不再给它投信。"""
        ...


async def post_check_in(
    s: AsyncSession, queue: TaskQueue, owner_session_id: str, source: str, text: str
) -> bool:
    """在调用方事务里给发起会话投一封检查信件；这个来源已有信件排着时不投。返回是否投了。"""
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


async def halt_all(works: Sequence[SessionWork], owner_session_id: str) -> None:
    """停下会话发起的全部异步工作（Consumer 停止会话时先调用：之后结束的工作不再投信）。"""
    for work in works:
        await work.halt(owner_session_id)
