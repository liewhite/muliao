"""任务队列的保留策略：启动时与之后每天一次，删掉 7 天前已完成的投递任务（event、deliver），tasks 表不随时间无限
增长。为什么只删这两种见 docs/architecture.md 3.2「保留」。
"""

import logging
from datetime import timedelta

import anyio

from muliao.platform.tables import TaskKind
from muliao.platform.tasks import TaskQueue

log = logging.getLogger(__name__)

PRUNABLE = (TaskKind.EVENT, TaskKind.DELIVER)
KEEP = timedelta(days=7)
_INTERVAL_SECONDS = 24 * 3600


class TaskRetention:
    def __init__(self, queue: TaskQueue):
        self._queue = queue

    async def prune(self) -> int:
        deleted = await self._queue.prune_done(PRUNABLE, finished_before=self._queue.now() - KEEP)
        log.info("pruned finished tasks", extra={"deleted": deleted, "kinds": list(PRUNABLE)})
        return deleted

    async def run_forever(self) -> None:
        """启动时先清一次再按天循环：服务常重启，等满一天才清可能一直清不到。"""
        while True:
            await self.prune()
            await anyio.sleep(_INTERVAL_SECONDS)
