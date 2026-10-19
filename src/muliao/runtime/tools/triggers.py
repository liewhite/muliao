"""触发器的管理：一组 trigger_* 工具覆盖所有触发方式（定时、webhook……），方式的差异只在 kind 与 config。

agent 按名称指定，不出现 id。webhook 的 token 只能由用户在界面上生成，agent 拿不到也不需要。
"""

from typing import Any

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents import AgentService
from muliao.models import Trigger
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.triggers.service import TriggerService


class TriggerKindInfo(BaseModel):
    kind: str
    description: str


class TriggerInfo(BaseModel):
    name: str
    kind: str
    agent: str
    prompt: str
    config: dict[str, Any]
    # 这种方式的运行状态，如下次触发时间、webhook 地址与是否已有 token
    status: dict[str, Any]
    enabled: bool
    last_error: str | None


def triggers_toolset(triggers: TriggerService, agents: AgentService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    async def info(t: Trigger, names: dict[str, str]) -> TriggerInfo:
        return TriggerInfo(
            name=t.name,
            kind=t.kind,
            agent=names[t.agent_id],
            prompt=t.prompt,
            config=t.config,
            status=triggers.kind(t.kind).status(t),
            enabled=t.enabled,
            last_error=t.last_error,
        )

    @ts.tool_plain
    async def trigger_kinds() -> list[TriggerKindInfo]:
        """List the kinds of triggers and the `config` each one takes."""
        return [TriggerKindInfo(kind=k.name, description=k.description) for k in triggers.kinds.values()]

    @ts.tool_plain
    async def trigger_list() -> list[TriggerInfo]:
        """List all triggers: things that start a new conversation of an agent (schedules, webhooks, ...)."""
        names = {a.id: a.name for a in await agents.list()}
        return [await info(t, names) for t in await triggers.list()]

    @ts.tool
    async def trigger_create(
        ctx: RunContext[RunScope],
        name: str,
        kind: str,
        prompt: str,
        config: dict[str, Any],
        agent: str | None = None,
    ) -> str:
        """Create a trigger that starts a new conversation of an agent with `prompt` (plus the event, if the kind
        carries one). See `trigger_kinds` for the kinds and their `config`. Triggered runs do not notify the user
        automatically: the prompt should say when to call `notify_user`. A webhook only works after the user
        generates its token in the app; tell the user to do that.

        Args:
            name: Lowercase letters, digits and `-`, e.g. `daily-report` (used in webhook URLs).
            kind: Trigger kind from `trigger_kinds`.
            prompt: What to do on each firing.
            config: Kind-specific settings, e.g. {"cron": "0 9 * * *"} for a schedule.
            agent: Name of the agent to run (see `list_agents`); defaults to you.
        """
        agent_id = (await agents.find_by_name(agent)).id if agent is not None else ctx.deps.agent_id
        trigger = await triggers.create(name=name, agent_id=agent_id, kind=kind, prompt=prompt, config=config)
        return f"created {trigger.kind} trigger {trigger.name}: {triggers.kind(trigger.kind).status(trigger)}"

    @ts.tool_plain
    async def trigger_update(
        name: str,
        prompt: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
    ) -> str:
        """Change a trigger; only the given fields change. Disable it to pause without losing it.

        Args:
            name: Trigger name.
            prompt: New instructions for each firing.
            config: New kind-specific settings (replaces the old config).
            enabled: Pause (false) or resume (true).
        """
        trigger = await triggers.get_by_name(name)
        trigger = await triggers.update(trigger.id, prompt=prompt, config=config, enabled=enabled)
        status = triggers.kind(trigger.kind).status(trigger)
        return f"updated trigger {name} ({'enabled' if trigger.enabled else 'paused'}): {status}"

    @ts.tool_plain
    async def trigger_run(name: str) -> str:
        """Fire a trigger now (without an event body), starting a new conversation of its agent.

        Args:
            name: Trigger name.
        """
        trigger = await triggers.get_by_name(name)
        await triggers.fire(trigger.id)
        return f"fired trigger {name}"

    @ts.tool_plain
    async def trigger_delete(name: str) -> str:
        """Delete a trigger.

        Args:
            name: Trigger name.
        """
        trigger = await triggers.get_by_name(name)
        await triggers.delete(trigger.id)
        return f"deleted trigger {name}"

    return ts
