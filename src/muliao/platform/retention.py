"""任务队列的保留策略：启动时与之后每天一次，删掉 7 天前已完成的投递类任务（种类由组合根给出：事件投递与
推送），tasks 表不随时间无限增长。为什么只删这几种见 docs/architecture.md 3.2「保留」。
"""

import logging
from collections.abc import Collection
from datetime import timedelta

import anyio

from muliao.platform.tasks import TaskQueue

log = logging.getLogger(__name__)

KEEP = timedelta(days=7)
_INTERVAL_SECONDS = 24 * 3600


class TaskRetention:
    def __init__(self, queue: TaskQueue, prunable: Collection[str]):
        self._queue = queue
        self._prunable = tuple(prunable)

    async def prune(self) -> int:
        deleted = await self._queue.prune_done(self._prunable, finished_before=self._queue.now() - KEEP)
        log.info("pruned finished tasks", extra={"deleted": deleted, "kinds": list(self._prunable)})
        return deleted

    async def run_forever(self) -> None:
        """启动时先清一次再按天循环：服务常重启，等满一天才清可能一直清不到。"""
        while True:
            await self.prune()
            await anyio.sleep(_INTERVAL_SECONDS)
