"""后台任务（工作区领域 JobRunner）：用户在会话页的等待项里结束一个后台任务，同 agent 的 job_kill。"""

from fastapi import APIRouter, Response

from muliao.api.deps import C

router = APIRouter()


@router.post("/agents/{agent_id}/jobs/{job_id}/kill", status_code=202, response_class=Response)
async def kill_job(c: C, agent_id: str, job_id: int) -> None:
    """结束这个 agent 的一个运行中的后台任务；结局（被结束）照常作为事件回到发起它的会话。已经结束的 409。"""
    await c.jobs.kill(job_id, agent_id)
