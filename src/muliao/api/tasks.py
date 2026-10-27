"""任务页：平台任务队列的列表与手动重试（Container.tasks 是只转发这两个方法的 TaskAdmin）。"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.notifications.api import DELIVER_TASK
from muliao.platform.tables import EVENT_TASK, TaskStatus
from muliao.platform.tasks import TaskView
from muliao.sessions.api import COMPACTION_TASK, RUN_TASK

router = APIRouter()


# 任务种类：各领域在自己的 api 导出，这里组成接口契约（tests 检查它恰好是 worker 登记的种类）。进 OpenAPI 后前端
# 按种类判断时用生成的类型（web/src/events.ts 的 TaskKind），后端改名时前端编译失败，而不是悄悄不再匹配
TaskKind = Literal[EVENT_TASK, RUN_TASK, COMPACTION_TASK, DELIVER_TASK]


class TaskOut(BaseModel):
    id: int
    kind: TaskKind
    agent_id: str | None
    session_id: str | None
    status: TaskStatus
    attempts: int
    error: str | None
    not_before: datetime
    created_at: datetime
    finished_at: datetime | None

    @classmethod
    def of(cls, t: TaskView) -> "TaskOut":
        """任务的只读快照（任务页经 TaskAdmin，会话页的最近一次运行经会话领域查到）。"""
        return cls(
            id=t.id,
            kind=t.kind,
            agent_id=t.agent_id,
            session_id=t.session_id,
            status=t.status,
            attempts=t.attempts,
            error=t.error,
            not_before=t.not_before,
            created_at=t.created_at,
            finished_at=t.finished_at,
        )


@router.get("/tasks")
async def list_tasks(
    c: C, status: TaskStatus | None = None, limit: int = Query(100, ge=1, le=500)
) -> list[TaskOut]:
    return [TaskOut.of(t) for t in await c.tasks.list(status=status, limit=limit)]


@router.post("/tasks/{task_id}/retry")
async def retry_task(c: C, task_id: int) -> TaskOut:
    return TaskOut.of(await c.tasks.retry(task_id))
