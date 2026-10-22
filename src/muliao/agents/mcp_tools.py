"""MCP server 的管理：agent 根据用户提供的信息自己接入 MCP。"""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.mcp import McpRegistry, McpTransport, mcp_config
from muliao.agents.ownership import Scope, owner_of, scope_of
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
    # 是否在给你的工具里：已启用，且没有被同名私有 server 遮蔽
    active: bool


class McpListing(BaseModel):
    servers: list[McpInfo]
    # 配置文件坏了、读不出来的 server（谁的、叫什么、原因）：删掉重加
    corrupt: list[str]


def mcp_admin_toolset(registry: McpRegistry) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def mcp_list(ctx: RunContext[RunScope]) -> McpListing:
        """List your private MCP servers and the public ones, including disabled ones. `active` tells whether its
        tools are offered to you. Values of env/headers are hidden. `corrupt` lists servers whose stored config
        cannot be read; remove and re-add them."""
        agent = ctx.deps.agent_id
        # 哪些真正生效由合并规则决定（registry.listing），这里不复述规则
        listing, corrupt = registry.listing(agent)
        servers = [
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
            for s, active in listing
        ]
        return McpListing(servers=servers, corrupt=[c.describe() for c in corrupt])

    @ts.tool
    async def mcp_add(
        ctx: RunContext[RunScope],
        name: str,
        transport: Literal["stdio", "http"],
        command: str | None = None,
        # 数组与对象参数不可为空、默认空值：可为空时 schema 是 anyOf[…, null]，模型更容易把它们写成 JSON 字符串
        args: tuple[str, ...] = (),
        url: str | None = None,
        # Mapping：只读，默认的 {} 不会被改动（下面转成 tuple）
        env: Mapping[str, str] = {},  # noqa: B006
        headers: Mapping[str, str] = {},  # noqa: B006
        scope: Scope = "private",
    ) -> str:
        """Register an MCP server. The server is connected once to verify it and list its tools;
        nothing is saved if that fails. Its tools become available from your next run, prefixed `<name>_`.
        Never put secrets in plain text: keep them in the vault and reference them as `${vault:KEY}`
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
        config = mcp_config(
            transport=McpTransport(transport),
            command=command,
            args=list(args),
            url=url,
            env=env,
            headers=headers,
        )
        _, tools = await registry.add(
            owner_of(scope, ctx.deps.agent_id), name, config, probe=True, author=ctx.deps.agent_id
        )
        return f"registered {scope} MCP server {name} with tools: {', '.join(tools) or '(none)'}"

    @ts.tool
    async def mcp_remove(ctx: RunContext[RunScope], name: str, scope: Scope = "private") -> str:
        """Remove an MCP server.

        Args:
            name: Server name.
            scope: `private` or `public`.
        """
        await registry.remove(owner_of(scope, ctx.deps.agent_id), name, ctx.deps.agent_id)
        return f"removed {scope} MCP server {name}"

    @ts.tool
    async def mcp_set_enabled(
        ctx: RunContext[RunScope], name: str, enabled: bool, scope: Scope = "private"
    ) -> str:
        """Enable or disable an MCP server without deleting its configuration. Takes effect from the next turn.

        Args:
            name: Server name.
            enabled: Whether its tools are offered.
            scope: `private` or `public`.
        """
        await registry.set_enabled(owner_of(scope, ctx.deps.agent_id), name, enabled, ctx.deps.agent_id)
        return f"{'enabled' if enabled else 'disabled'} {scope} MCP server {name}"

    return ts
