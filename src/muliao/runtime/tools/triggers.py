"""触发器的管理：cron 定时任务。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents import AgentService
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.triggers.cron import CronService


class CronInfo(BaseModel):
    name: str
    agent: str
    schedule: str
    prompt: str
    enabled: bool
    next_run_at: str


def cron_toolset(cron: CronService, agents: AgentService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def cron_list() -> list[CronInfo]:
        """List all scheduled jobs."""
        names = {a.id: a.name for a in await agents.list()}
        return [
            CronInfo(
                name=j.name,
                agent=names[j.agent_id],
                schedule=j.schedule,
                prompt=j.prompt,
                enabled=j.enabled,
                next_run_at=j.next_run_at.isoformat(),
            )
            for j in await cron.list()
        ]

    @ts.tool
    async def cron_create(
        ctx: RunContext[RunScope], name: str, schedule: str, prompt: str, agent: str | None = None
    ) -> str:
        """Schedule a recurring job. Each firing starts a new session of the agent with `prompt`.
        Fired sessions do not notify the user automatically: the prompt should say when to call `notify_user`.

        Args:
            name: Unique job name.
            schedule: 5-field cron expression in the server's configured timezone, e.g. `*/5 * * * *`.
            prompt: Instructions for each run.
            agent: Name of the agent to run (see `list_agents`); defaults to you.
        """
        job = await cron.create(
            name=name,
            agent_id=(await agents.find_by_name(agent)).id if agent is not None else ctx.deps.agent_id,
            schedule=schedule,
            prompt=prompt,
        )
        return f"scheduled {job.name}, next run at {job.next_run_at.isoformat()}"

    @ts.tool_plain
    async def cron_delete(name: str) -> str:
        """Delete a scheduled job.

        Args:
            name: Job name.
        """
        job = await cron.get_by_name(name)
        await cron.delete(job.id)
        return f"deleted cron job {name}"

    return ts
