"""触发器的管理：一组 trigger_* 工具覆盖所有触发方式（定时、webhook……），方式的差异只在 kind 与 config。

agent 按名称指定，不出现 id。webhook 的 token 只能由用户在界面上生成，agent 拿不到也不需要。
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import AgentNames, AgentService
from muliao.errors import InvalidInput
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.platform.values import KEEP
from muliao.triggers.precheck import Precheck
from muliao.triggers.service import TriggerService
from muliao.triggers.tables import Trigger


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
    precheck: Precheck | None
    # 被自动停用的原因；最近一次前置检查失败的原因
    disabled_reason: str | None
    precheck_error: str | None
    # 最近一次触发开出的会话；已归档的（跑完的一次性触发器）在这里能看到它的结果去了哪
    last_session_id: str | None
    # 不为空表示它已经跑完、不会再触发了
    finished_at: datetime | None


_CREATE_DESCRIPTION = """\
Create a trigger that starts a new conversation of an agent with `prompt` (plus the event, if the kind \
carries one). See `trigger_kinds` for the kinds and their `config`. Triggered runs do not notify the user \
automatically: the prompt should say when to notify the user (`notify_user`, so that agent needs the notify \
tools). A webhook only works after the user generates its token in the app; tell the user to do that.

For frequent checks (e.g. every minute) add a `precheck`, so the agent only runs when something happened: a \
bash command run in the agent's workspace before each firing, with the event body (webhook) on stdin. \
With fire_when "output", exit 0 with output fires and the output becomes the event the agent sees, exit 0 \
with no output skips; with "changed", it fires only when the output differs from the last run and the \
agent gets the diff plus the output (the first run only records a baseline). A non-zero exit, or running \
longer than {timeout:g} seconds (the command is then killed), is recorded as the trigger's error; when the \
check goes from healthy to failing, the trigger's agent gets one new conversation with the error to fix it \
(`trigger_run` only reports a failure back to its caller). Keep the precheck cheap and well under that \
limit, retries included; slow work (full scans, long retry loops) belongs in the agent's run, not in the \
precheck."""


def triggers_toolset(
    triggers: TriggerService, agents: AgentService, precheck_timeout: float
) -> FunctionToolset[RunScope]:
    """precheck_timeout：前置检查的时限（runtime.precheck_timeout_seconds），写进工具说明，agent 写检查脚本时要知道。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    async def info(t: Trigger, names: AgentNames) -> TriggerInfo:
        view = triggers.view(t)
        return TriggerInfo(
            name=t.name,
            kind=t.kind,
            agent=names[t.agent_id],
            prompt=t.prompt,
            config=t.config,
            status=view.status.model_dump(mode="json"),
            enabled=t.enabled,
            precheck=view.precheck,
            disabled_reason=t.disabled_reason,
            precheck_error=t.precheck_error,
            last_session_id=t.last_session_id,
            finished_at=t.finished_at,
        )

    @ts.tool_plain
    async def trigger_kinds() -> list[TriggerKindInfo]:
        """List the kinds of triggers and the `config` each one takes. Returns a JSON array, each item with `kind`
        and `description`."""
        return [TriggerKindInfo(kind=k.name, description=k.description) for k in triggers.kinds.values()]

    @ts.tool_plain
    async def trigger_list(finished: bool = False) -> list[TriggerInfo]:
        """List triggers: things that start a new conversation of an agent (schedules, webhooks, ...). Returns a
        JSON array of triggers, each with `name`, `kind`, `agent`, `prompt`, `config`, `status` (kind-specific
        state such as the next firing time or the webhook URL), `enabled`, `precheck`, `disabled_reason`,
        `precheck_error`, `last_session_id` and `finished_at` (ISO 8601; set once it will not fire again).

        Args:
            finished: List the archived ones instead — `once` triggers that already ran and are gone from
                the list above. Their `last_session_id` is the conversation they started, so this is how you
                look up what came of a one-off task.
        """
        names = await agents.names()
        return [await info(t, names) for t in await triggers.list(finished=finished)]

    @ts.tool(description=_CREATE_DESCRIPTION.format(timeout=precheck_timeout))
    async def trigger_create(
        ctx: RunContext[RunScope],
        name: str,
        kind: str,
        prompt: str,
        config: dict[str, Any],
        agent: str | None = None,
        precheck: Precheck | None = None,
    ) -> str:
        """The description is `_CREATE_DESCRIPTION` (it carries the configured precheck time limit).

        Args:
            name: Lowercase letters, digits and `-`, e.g. `daily-report` (used in webhook URLs).
            kind: Trigger kind from `trigger_kinds`.
            prompt: What to do on each firing.
            config: Kind-specific settings, e.g. {"cron": "0 9 * * *"} for a schedule, {"at":
                "2026-10-22T10:30"} for a one-off `once` trigger (never a cron expression with a date:
                that fires again every year and stays in the list).
            agent: Name of the agent to run; defaults to you.
            precheck: Optional command deciding whether to fire; see above.
        """
        agent_id = (await agents.find_by_name(agent)).id if agent is not None else ctx.deps.agent_id
        trigger = await triggers.create(
            name=name, agent_id=agent_id, kind=kind, prompt=prompt, config=config, precheck=precheck
        )
        return f"created {trigger.kind} trigger {trigger.name}; status: {triggers.view(trigger).status.model_dump_json()}"

    @ts.tool_plain
    async def trigger_update(
        name: str,
        prompt: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
        precheck: Precheck | None = None,
        remove_precheck: bool = False,
    ) -> str:
        """Change a trigger; only the given fields change. Disable it to pause without losing it.

        Args:
            name: Trigger name.
            prompt: New instructions for each firing.
            config: New kind-specific settings (replaces the old config).
            enabled: Pause (false) or resume (true).
            precheck: New precheck (replaces the old one and resets its baseline); see `trigger_create`.
            remove_precheck: Remove the precheck so every firing runs the agent.
        """
        if precheck is not None and remove_precheck:
            raise InvalidInput("give either precheck or remove_precheck, not both")
        trigger = await triggers.get_by_name(name)
        trigger = await triggers.update(
            trigger.id,
            prompt=prompt,
            config=config,
            enabled=enabled,
            precheck=None if remove_precheck else KEEP if precheck is None else precheck,
        )
        status = triggers.view(trigger).status.model_dump_json()
        return f"updated trigger {name} ({'enabled' if trigger.enabled else 'paused'}); status: {status}"

    @ts.tool_plain
    async def trigger_run(name: str) -> str:
        """Fire a trigger now (without an event body), starting a new conversation of its agent. Its precheck,
        if any, runs first, so this also tests the precheck. This does not use up a `once` trigger: it still
        fires at its own time.

        Args:
            name: Trigger name.
        """
        trigger = await triggers.get_by_name(name)
        if await triggers.run(trigger.id) is None:
            return f"precheck of {name} found nothing to do (or recorded its baseline): not fired"
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
