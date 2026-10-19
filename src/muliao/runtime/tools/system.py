"""与系统其余部分交互的工具：委派、通知、MCP 管理、vault、cron。"""

from typing import Literal

from pydantic import BaseModel
from pydantic_ai import CallDeferred, FunctionToolset, ModelRetry, RunContext

from muliao.agents import AgentService
from muliao.db import Database
from muliao.delivery.base import Notification, Notifier
from muliao.models import McpTransport, ReplyTo
from muliao.ownership import Scope, owner_of, scope_of
from muliao.runtime.conversation import Threads, validate_thread
from muliao.runtime.mcp import McpRegistry, McpSpec
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.triggers.cron import CronService
from muliao.vault import Vault


class AgentInfo(BaseModel):
    name: str
    description: str


def agents_toolset(db: Database, agents: AgentService, threads: Threads) -> FunctionToolset[RunScope]:
    """委派：thread 是这个会话开出的、以名字区分的子会话，同名即续聊（保留对方上下文）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentInfo]:
        """List all agents and what each one is for. Use before delegating."""
        return [AgentInfo(name=a.name, description=a.description) for a in await agents.list()]

    @ts.tool
    async def delegate_task(ctx: RunContext[RunScope], agent: str, task: str, thread: str) -> str:
        """Hand a task to an agent and keep going without waiting. It runs in a thread the user can see and
        reply in; the user is notified with its answer. Call `wait_for(thread)` if you need the answer yourself
        before you finish; if you end your turn instead, the answer is NOT sent back to you.

        Args:
            agent: Target agent name from `list_agents`.
            task: Self-contained task description; the agent cannot see this conversation except through
                earlier messages in the same thread.
            thread: Short name of the thread, e.g. "weekly-report". Reusing a thread name continues that
                conversation with its full context; use a new name for an unrelated task.
        """
        parent_id = _require_session(ctx)
        target = await agents.find_by_name(agent)
        async with db.transaction() as s:
            await threads.send(
                s,
                parent_id=parent_id,
                agent=target,
                thread=thread,
                prompt=task,
                private=False,
                reply_to=ReplyTo.USER,
            )
        thread = validate_thread(thread)
        return f"delegated to {target.name} in thread {thread!r}; call wait_for({thread!r}) if you need the answer"

    @ts.tool
    async def ask_agent(ctx: RunContext[RunScope], agent: str, message: str, thread: str) -> str:
        """Ask an agent and wait for its answer, which is returned as this tool's result. The thread is private
        to this conversation (the user can read but not reply). Ask again in the same thread to follow up with
        the agent's context intact, e.g. review -> fix -> review again. Several asks in one response run in parallel.

        Args:
            agent: Target agent name from `list_agents`.
            message: Self-contained request; the agent sees only this thread's history.
            thread: Short name of the thread, e.g. "code-review". Reuse it to continue the same conversation.
        """
        parent_id = _require_session(ctx)
        target = await agents.find_by_name(agent)
        thread = validate_thread(thread)
        if not message.strip():
            raise ModelRetry("message must not be empty")
        # 现在就占用 thread：同一响应里并行的调用若争用同名 thread，冲突在这里作为可纠正的错误交回模型，
        # 而不是到提交时才让整轮失败
        async with db.transaction() as s:
            await threads.open(s, parent_id=parent_id, agent=target, thread=thread, private=True)
        raise CallDeferred(
            metadata={"action": "ask", "agent_id": target.id, "thread": thread, "message": message}
        )

    @ts.tool
    async def wait_for(ctx: RunContext[RunScope], thread: str) -> str:
        """Wait for the latest task in a thread of this conversation to finish and return its answer
        (prefixed with "[failed]" if it failed). Returns at once if it has already finished.

        Args:
            thread: Thread name used with `delegate_task` or `ask_agent`.
        """
        parent_id = _require_session(ctx)
        async with db.transaction() as s:
            child = await threads.find(s, parent_id, thread)
            if child is None:
                raise ModelRetry(f"this conversation has no thread named {thread!r}")
            state = await threads.state(s, child)
        match state.status:
            case "done":
                return state.text
            case "failed":
                return f"[failed] {state.text}"
            case "running":
                raise CallDeferred(metadata={"action": "wait", "turn_id": state.turn_id})
            case "idle":
                raise ModelRetry(f"thread {thread!r} has no task to wait for")

    return ts


def _require_session(ctx: RunContext[RunScope]) -> str:
    if ctx.deps.session_id is None:
        raise ModelRetry("delegation is only available inside a conversation")
    return ctx.deps.session_id


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
    scope: Scope
    description: str


def vault_toolset(vault: Vault) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def vault_list(ctx: RunContext[RunScope]) -> list[SecretKeyInfo]:
        """List secret keys you can use: your private ones and public ones (a private key hides a public key
        of the same name). Values are never shown."""
        return [
            SecretKeyInfo(key=s.key, scope=scope_of(s.owner), description=s.description)
            for s in await vault.visible_to(ctx.deps.agent_id)
        ]

    @ts.tool
    async def vault_set(
        ctx: RunContext[RunScope], key: str, value: str, description: str, scope: Scope = "private"
    ) -> str:
        """Store a secret the user gave you so it never has to appear in files, skills, memory or MCP configs.
        The value is redacted from the saved conversation.
        Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])`.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
            scope: `private` (only you) or `public` (every agent, and public MCP servers).
        """
        await vault.set(owner_of(scope, ctx.deps.agent_id), key, value, description)
        return f"stored {scope} secret {key}"

    @ts.tool
    async def vault_delete(ctx: RunContext[RunScope], key: str, scope: Scope = "private") -> str:
        """Delete a secret that is no longer needed.

        Args:
            key: The secret key.
            scope: `private` or `public`.
        """
        await vault.delete(owner_of(scope, ctx.deps.agent_id), key)
        return f"deleted {scope} secret {key}"

    return ts


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
