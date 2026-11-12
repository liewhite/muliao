"""任务页：平台任务队列的列表与手动重试（Container.tasks 是只转发这两个方法的 TaskAdmin）。"""

from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.platform.tables import TaskStatus
from muliao.platform.tasks import TaskView

router = APIRouter()


class TaskOut(BaseModel):
    id: int
    kind: str
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
