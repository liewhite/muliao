"""任务队列的保留策略：每天删一次 7 天前已完成的投递任务（event、deliver），tasks 表不随时间无限增长。设计见
docs/architecture.md「任务队列即 actor 调度」。

只删这两种：它们完成后没有别的数据引用。run 不能删：信件的 mail.consumed_by 指向它且 ON DELETE CASCADE，删了会
连带删掉它处理过的信件与幂等键；reflection、compaction 量小，留着。failed 一律保留，供排查与人工重试。
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
