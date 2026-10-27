"""信箱对账（docs/architecture.md「会话信箱」）：定期补做没能及时完成的两件事——最终失败的运行没能结清的委派
（settlement.reconcile_failures），以及有信件在排队却没被唤醒的会话（mailbox.wake_in）。"""

from __future__ import annotations

import logging

import anyio

from muliao.platform.db import Database
from muliao.platform.tasks import TaskQueue
from muliao.sessions.directory import among_in
from muliao.sessions.mailbox import wake_in, with_queued_in
from muliao.sessions.settlement import reconcile_failures

log = logging.getLogger(__name__)

_RECONCILE_SECONDS = 30.0


class MailReconciler:
    """定期对账：最终失败的运行没能结清的委派（收尾钩子失败）、有信件在排队却没有被及时唤醒的会话（投递时的事务
    失败、进程重启），按各自唯一的规则补上。"""

    def __init__(self, db: Database, queue: TaskQueue, interval_seconds: float = _RECONCILE_SECONDS):
        self._db = db
        self._queue = queue
        self._interval = interval_seconds

    async def reconcile(self) -> int:
        """补投递的运行数。先补做失败的结清再唤醒：交给过失败运行、还排着的委派信件先退出信箱，不被唤醒的新运行
        取走。"""
        settled = await reconcile_failures(self._db, self._queue)
        if settled:
            # 只有收尾钩子失败时才会走到这里
            log.warning("reconcile settled delegations of failed runs", extra={"session_ids": settled})
        async with self._db.transaction() as s:
            ids = await with_queued_in(s)
            sessions = await among_in(s, ids)
            woken = [x for x in ids if await wake_in(s, self._queue, sessions[x])]
        if woken:
            # 投信时与运行结束（含最终失败）时都已唤醒过；到这里多半是那次没做成（事务失败、重启），补了什么要可查
            log.info("reconcile woke consumers for queued mail", extra={"session_ids": woken})
        return len(woken)

    async def run_forever(self) -> None:
        while True:
            await self.reconcile()
            await anyio.sleep(self._interval)
