"""触发器的管理：cron 定时任务与外部事件触发器。agent 按名称指定，不出现 id。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents import AgentService
from muliao.errors import Conflict
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.triggers.cron import CronService
from muliao.triggers.events import EventTriggers, hook_url, validate_trigger_name
from muliao.vault import Vault


def token_key(trigger_name: str) -> str:
    """agent 创建或重置的事件 token 存在它私有 vault 的这个 key 下。触发器名不含下划线，所以一一对应。"""
    return "EVENT_TOKEN_" + trigger_name.upper().replace("-", "_")


class EventTriggerInfo(BaseModel):
    name: str
    agent: str
    url: str
    prompt: str
    enabled: bool


class CronInfo(BaseModel):
    name: str
    agent: str
    schedule: str
    prompt: str
    enabled: bool
    next_run_at: str


def triggers_toolset(
    cron: CronService, events: EventTriggers, agents: AgentService, vault: Vault, public_url: str
) -> FunctionToolset[RunScope]:
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

    @ts.tool_plain
    async def cron_update(
        name: str, schedule: str | None = None, prompt: str | None = None, enabled: bool | None = None
    ) -> str:
        """Change a scheduled job; only the given fields change. Disable a job to pause it without losing it.

        Args:
            name: Job name.
            schedule: New 5-field cron expression.
            prompt: New instructions for each run.
            enabled: Pause (false) or resume (true).
        """
        job = await cron.get_by_name(name)
        job = await cron.update(job.id, schedule=schedule, prompt=prompt, enabled=enabled)
        state = f"next run at {job.next_run_at.isoformat()}" if job.enabled else "paused"
        return f"updated cron job {name}, {state}"

    @ts.tool_plain
    async def event_trigger_list() -> list[EventTriggerInfo]:
        """List external event triggers: URLs external systems POST events to."""
        names = {a.id: a.name for a in await agents.list()}
        return [
            EventTriggerInfo(
                name=t.name,
                agent=names[t.agent_id],
                url=hook_url(public_url, t.name),
                prompt=t.prompt,
                enabled=t.enabled,
            )
            for t in await events.list()
        ]

    @ts.tool
    async def event_trigger_create(
        ctx: RunContext[RunScope], name: str, prompt: str, agent: str | None = None
    ) -> str:
        """Create a URL that external systems (webhooks, scripts) POST events to. Each event starts a new
        session of the agent with `prompt` followed by the event body; the response carries a `session_id`
        that the sender may pass back as `?session_id=` to continue that session. Events do not notify the
        user automatically: the prompt should say when to call `notify_user`.

        The token is never shown: it is stored in your private vault under the returned key
        (`EVENT_TOKEN_<NAME>`). Configure the sender with header `Authorization: Bearer <token>`, e.g. via
        `run_shell(secrets=[key])`. If storing it fails, call `event_trigger_rotate_token` to get a new one.

        Args:
            name: Lowercase letters, digits and `-`, used in the URL, e.g. `github-issues`.
            prompt: What these events are and how to handle them.
            agent: Name of the agent that handles the events (see `list_agents`); defaults to you.
        """
        agent_id = (await agents.find_by_name(agent)).id if agent is not None else ctx.deps.agent_id
        key = token_key(validate_trigger_name(name))
        # 不覆盖已有的 secret：在创建触发器之前就报错
        if await vault.has(ctx.deps.agent_id, key):
            raise Conflict(f"your vault already has {key}; delete it first or choose another trigger name")
        trigger, token = await events.create(name=name, agent_id=agent_id, prompt=prompt)
        await vault.set(ctx.deps.agent_id, key, token, f"bearer token of event trigger {trigger.name}")
        return f"created event trigger {trigger.name}: POST {hook_url(public_url, trigger.name)}; token in vault key {key}"

    @ts.tool
    async def event_trigger_rotate_token(ctx: RunContext[RunScope], name: str) -> str:
        """Replace an event trigger's token: the old one stops working at once, wherever it was stored. The new
        one is stored in your private vault as `EVENT_TOKEN_<NAME>`, replacing what was there.

        Args:
            name: Trigger name.
        """
        trigger = await events.get_by_name(name)
        token = await events.rotate_token(trigger.id)
        key = token_key(trigger.name)
        await vault.set(ctx.deps.agent_id, key, token, f"bearer token of event trigger {trigger.name}")
        return f"rotated token of {trigger.name}; new token in your vault key {key}; any other copy is now invalid"

    @ts.tool_plain
    async def event_trigger_update(name: str, prompt: str | None = None, enabled: bool | None = None) -> str:
        """Change an event trigger; only the given fields change. A disabled trigger rejects events.

        Args:
            name: Trigger name.
            prompt: New handling instructions.
            enabled: Accept (true) or reject (false) events.
        """
        trigger = await events.get_by_name(name)
        await events.update(trigger.id, prompt=prompt, enabled=enabled)
        return f"updated event trigger {name}"

    @ts.tool
    async def event_trigger_delete(ctx: RunContext[RunScope], name: str) -> str:
        """Delete an event trigger; its URL stops accepting events. Its token in your private vault is deleted too.

        Args:
            name: Trigger name.
        """
        trigger = await events.get_by_name(name)
        await events.delete(trigger.id)
        key = token_key(trigger.name)
        if await vault.has(ctx.deps.agent_id, key):
            await vault.delete(ctx.deps.agent_id, key)
            return f"deleted event trigger {name} and its token {key} from your vault"
        return f"deleted event trigger {name}"

    return ts
