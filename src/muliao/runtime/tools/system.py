"""与系统其余部分交互的工具：委派、通知、MCP 管理、vault、cron。"""

from typing import Literal

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents import AgentService
from muliao.delivery.base import Notification, Notifier
from muliao.models import DeliveryChannel, McpTransport, Origin
from muliao.ownership import Scope, owner_of, scope_of
from muliao.runtime.mcp import McpRegistry, McpSpec
from muliao.runtime.scope import RunScope
from muliao.runtime.sessions import SessionService
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.triggers.cron import CronService
from muliao.vault import Vault


class AgentInfo(BaseModel):
    id: str
    name: str
    description: str


def agents_toolset(agents: AgentService, sessions: SessionService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentInfo]:
        """List all agents (id, display name, purpose). Use before delegating; refer to agents by `id`."""
        return [AgentInfo(id=a.id, name=a.name, description=a.description) for a in await agents.list()]

    @ts.tool
    async def delegate_task(ctx: RunContext[RunScope], agent_id: str, task: str) -> str:
        """Hand a task to an agent. It runs asynchronously in a new session of that agent,
        and the user is notified when it finishes. Include all context the agent needs: it cannot see this conversation.

        Args:
            agent_id: Target agent `id` from `list_agents`.
            task: Self-contained task description.
        """
        target = await agents.get(agent_id)
        session = await sessions.start(
            agent_id=target.id,
            prompt=task,
            origin=Origin.DELEGATION,
            origin_ref=ctx.deps.session_id,
            delivery=DeliveryChannel.WEBPUSH,
        )
        return f"delegated to {target.name} ({target.id}), session id {session.id}"

    return ts


def notify_toolset(notifier: Notifier) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def notify_user(ctx: RunContext[RunScope], title: str, body: str) -> str:
        """Send a push notification to the user's devices. Tapping it opens this session.

        Args:
            title: Short title.
            body: Notification text, a few sentences at most.
        """
        await notifier.notify(
            ctx.deps.agent_id,
            Notification(
                channel=DeliveryChannel.WEBPUSH,
                target=None,
                title=title,
                body=body,
                session_id=ctx.deps.session_id,
            ),
        )
        return "notification queued"

    return ts


class McpInfo(BaseModel):
    name: str
    scope: Scope
    transport: str
    command: str | None
    args: list[str]
    url: str | None
    env_keys: list[str]
    header_keys: list[str]
    enabled: bool


def mcp_admin_toolset(registry: McpRegistry) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def mcp_list(ctx: RunContext[RunScope]) -> list[McpInfo]:
        """List MCP servers available to you (your private ones and public ones). Values of env/headers are hidden."""
        return [
            McpInfo(
                name=s.name,
                scope=scope_of(s.owner),
                transport=s.transport,
                command=s.command,
                args=s.args,
                url=s.url,
                env_keys=sorted(s.env),
                header_keys=sorted(s.headers),
                enabled=s.enabled,
            )
            for s in await registry.visible_to(ctx.deps.agent_id)
        ]

    @ts.tool
    async def mcp_add(
        ctx: RunContext[RunScope],
        name: str,
        transport: Literal["stdio", "http"],
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        scope: Scope = "private",
    ) -> str:
        """Register an MCP server. The server is connected once to verify it and list its tools;
        nothing is saved if that fails. Its tools become available from your next run, prefixed `<name>_`.
        Never put secrets in plain text: store them with `vault_set` and reference them as `${vault:KEY}`
        inside args, url, env or headers.

        Args:
            name: Lowercase slug.
            transport: `stdio` (local command) or `http` (streamable HTTP / SSE URL).
            command: Executable for stdio, e.g. `npx`.
            args: Arguments for stdio.
            url: Endpoint for http.
            env: Environment variables for stdio.
            headers: HTTP headers for http, e.g. {"Authorization": "Bearer ${vault:GITHUB_TOKEN}"}.
            scope: `private` (only you) or `public` (all agents).
        """
        spec = McpSpec(
            name=name,
            transport=McpTransport(transport),
            command=command,
            args=tuple(args or ()),
            url=url,
            env=tuple((env or {}).items()),
            headers=tuple((headers or {}).items()),
        )
        _, tools = await registry.add(owner_of(scope, ctx.deps.agent_id), spec, probe=True)
        return f"registered {scope} MCP server {name} with tools: {', '.join(tools) or '(none)'}"

    @ts.tool
    async def mcp_remove(ctx: RunContext[RunScope], name: str, scope: Scope = "private") -> str:
        """Remove an MCP server.

        Args:
            name: Server name.
            scope: `private` or `public`.
        """
        await registry.remove(owner_of(scope, ctx.deps.agent_id), name)
        return f"removed {scope} MCP server {name}"

    return ts


class SecretKeyInfo(BaseModel):
    key: str
    description: str


def vault_toolset(vault: Vault) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def vault_list() -> list[SecretKeyInfo]:
        """List secret keys stored in the vault (values are never shown)."""
        return [SecretKeyInfo(key=s.key, description=s.description) for s in await vault.list()]

    @ts.tool_plain
    async def vault_set(key: str, value: str, description: str) -> str:
        """Store a secret the user gave you so it never has to appear in files, skills, memory or MCP configs.
        The value is redacted from the saved conversation.
        Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])`.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
        """
        await vault.set(key, value, description)
        return f"stored secret {key}"

    return ts


class CronInfo(BaseModel):
    name: str
    agent_id: str
    schedule: str
    prompt: str
    enabled: bool
    next_run_at: str


def cron_toolset(cron: CronService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def cron_list() -> list[CronInfo]:
        """List all scheduled jobs."""
        return [
            CronInfo(
                name=j.name,
                agent_id=j.agent_id,
                schedule=j.schedule,
                prompt=j.prompt,
                enabled=j.enabled,
                next_run_at=j.next_run_at.isoformat(),
            )
            for j in await cron.list()
        ]

    @ts.tool
    async def cron_create(
        ctx: RunContext[RunScope], name: str, schedule: str, prompt: str, agent_id: str | None = None
    ) -> str:
        """Schedule a recurring job. Each firing starts a new session of the agent with `prompt`.
        Fired sessions do not notify the user automatically: the prompt should say when to call `notify_user`.

        Args:
            name: Unique job name.
            schedule: 5-field cron expression in the server's configured timezone, e.g. `*/5 * * * *`.
            prompt: Instructions for each run.
            agent_id: `id` of the agent to run (see `list_agents`); defaults to you.
        """
        job = await cron.create(
            name=name, agent_id=agent_id or ctx.deps.agent_id, schedule=schedule, prompt=prompt
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
