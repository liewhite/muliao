"""MCP server 的管理：agent 根据用户提供的信息自己接入 MCP。"""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.mcp import McpConfig, McpRegistry, McpTransport, mcp_config
from muliao.agents.ownership import Scope, scope_of
from muliao.agents.service import AgentService
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope


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
    # 是否在给你的工具里：已启用，且是你私有的或给你勾选了的公共的
    active: bool


def mcp_admin_toolset(registry: McpRegistry, agents: AgentService) -> FunctionToolset[RunScope]:
    """MCP 配置：私有的与公共的各用各的工具（公共的是 *_public，权限 mcp.config.public:*）。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def mcp_list(ctx: RunContext[RunScope]) -> list[McpInfo]:
        """List your private MCP servers and the public ones, including disabled ones. `active` tells whether its
        tools are offered to you (you get your private servers and the public ones selected for you). Values of
        env/headers are hidden."""
        agent = await agents.get(ctx.deps.agent_id)
        # 哪些真正生效由 registry.listing 决定，这里不复述规则
        return [
            McpInfo(
                name=s.name,
                scope=scope_of(s.owner),
                transport=s.transport,
                command=s.config.command,
                args=s.config.args,
                url=s.config.url,
                env_keys=sorted(s.config.env),
                header_keys=sorted(s.config.headers),
                enabled=s.enabled,
                active=active,
            )
            for s, active in await registry.listing(agent.id, agent.mcp_servers)
        ]

    async def add(ctx: RunContext[RunScope], owner: str | None, name: str, config: McpConfig) -> str:
        _, tools = await registry.add(owner, name, config, probe=True, author=ctx.deps.agent_id)
        return f"registered {scope_of(owner)} MCP server {name} with tools: {', '.join(tools) or '(none)'}"

    @ts.tool
    async def mcp_add(
        ctx: RunContext[RunScope],
        name: str,
        transport: Literal["stdio", "http"],
        command: str | None = None,
        # 数组与对象参数不可为空、默认空值：可为空时 schema 是 anyOf[…, null]，模型更容易把它们写成 JSON 字符串
        args: tuple[str, ...] = (),
        url: str | None = None,
        # Mapping：只读，默认的 {} 不会被改动
        env: Mapping[str, str] = {},  # noqa: B006
        headers: Mapping[str, str] = {},  # noqa: B006
    ) -> str:
        """Register a private MCP server (only you get its tools). The server is connected once to verify it and
        list its tools; nothing is saved if that fails. Its tools become available from your next run, prefixed
        `<name>_`. Never put secrets in plain text: keep them in the vault and reference them as `${vault:KEY}`
        inside args, url, env or headers.

        Names are unique across private and public servers.

        Args:
            name: Lowercase slug.
            transport: `stdio` (local command) or `http` (streamable HTTP / SSE URL).
            command: Executable for stdio, e.g. `npx`.
            args: Arguments for stdio.
            url: Endpoint for http.
            env: Environment variables for stdio.
            headers: HTTP headers for http, e.g. {"Authorization": "Bearer ${vault:GITHUB_TOKEN}"}.
        """
        config = mcp_config(
            transport=McpTransport(transport),
            command=command,
            args=list(args),
            url=url,
            env=dict(env),
            headers=dict(headers),
        )
        return await add(ctx, ctx.deps.agent_id, name, config)

    @ts.tool
    async def mcp_add_public(
        ctx: RunContext[RunScope],
        name: str,
        transport: Literal["stdio", "http"],
        command: str | None = None,
        args: tuple[str, ...] = (),
        url: str | None = None,
        env: Mapping[str, str] = {},  # noqa: B006
        headers: Mapping[str, str] = {},  # noqa: B006
    ) -> str:
        """Register a public MCP server. Agents get its tools only once it is selected for them (the user does
        that in the app, or `update_agent`'s `mcp_servers`). Verified, prefixed and configured like `mcp_add`;
        reference only public secrets in it.

        Args:
            name: Lowercase slug.
            transport: `stdio` (local command) or `http` (streamable HTTP / SSE URL).
            command: Executable for stdio, e.g. `npx`.
            args: Arguments for stdio.
            url: Endpoint for http.
            env: Environment variables for stdio.
            headers: HTTP headers for http.
        """
        config = mcp_config(
            transport=McpTransport(transport),
            command=command,
            args=list(args),
            url=url,
            env=dict(env),
            headers=dict(headers),
        )
        return await add(ctx, None, name, config)

    @ts.tool
    async def mcp_remove(ctx: RunContext[RunScope], name: str) -> str:
        """Remove one of your private MCP servers.

        Args:
            name: Server name.
        """
        await registry.remove(ctx.deps.agent_id, name, ctx.deps.agent_id)
        return f"removed private MCP server {name}"

    @ts.tool
    async def mcp_remove_public(ctx: RunContext[RunScope], name: str) -> str:
        """Remove a public MCP server.

        Args:
            name: Server name.
        """
        await registry.remove(None, name, ctx.deps.agent_id)
        return f"removed public MCP server {name}"

    @ts.tool
    async def mcp_set_enabled(ctx: RunContext[RunScope], name: str, enabled: bool) -> str:
        """Enable or disable one of your private MCP servers without deleting its configuration. Takes effect from
        the next turn.

        Args:
            name: Server name.
            enabled: Whether its tools are offered.
        """
        await registry.set_enabled(ctx.deps.agent_id, name, enabled, ctx.deps.agent_id)
        return f"{'enabled' if enabled else 'disabled'} private MCP server {name}"

    @ts.tool
    async def mcp_set_enabled_public(ctx: RunContext[RunScope], name: str, enabled: bool) -> str:
        """Enable or disable a public MCP server for every agent that has it selected, without deleting its
        configuration. Takes effect from the next turn.

        Args:
            name: Server name.
            enabled: Whether its tools are offered.
        """
        await registry.set_enabled(None, name, enabled, ctx.deps.agent_id)
        return f"{'enabled' if enabled else 'disabled'} public MCP server {name}"

    return ts
