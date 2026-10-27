"""MCP server 的管理：agent 根据用户提供的信息自己接入 MCP。启停只在界面上做。"""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.mcp import McpRegistry, McpTransport, mcp_config
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope


class McpInfo(BaseModel):
    name: str
    # 是你自己的（否则是角色或你自己的 allow 分给你的）
    yours: bool
    transport: str
    command: str | None
    args: list[str]
    url: str | None
    env_keys: list[str]
    header_keys: list[str]
    # 停用的 server 不连接（在界面上启停）
    enabled: bool


def mcp_admin_toolset(registry: McpRegistry) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def mcp_list(ctx: RunContext[RunScope]) -> list[McpInfo]:
        """List the MCP servers you can use: your own and those your roles or your own allow rules give you,
        including disabled ones (their tools are not offered; the user enables them in the app).
        Returns a JSON array of servers, each with `name`, `yours`, `transport`, `command`, `args`, `url`,
        `env_keys`, `header_keys` and `enabled`: only the keys of env/headers are shown, never their values."""
        me = ctx.deps.agent_id
        return [
            McpInfo(
                name=s.name,
                yours=s.owner == me,
                transport=s.transport,
                command=s.config.command,
                args=s.config.args,
                url=s.config.url,
                env_keys=sorted(s.config.env),
                header_keys=sorted(s.config.headers),
                enabled=s.enabled,
            )
            for s in await registry.usable(me)
        ]

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
        """Register an MCP server. It is yours and only you get its tools; other agents get it only through a
        role. The server is connected once to verify it and list its tools; nothing is saved if that fails. Its
        tools become available from your next run, prefixed `<name>_`. Never put secrets in plain text: keep
        them in the vault and reference them as `${vault:KEY}` inside args, url, env or headers (resolved with
        the secrets of the agent using the server). Names are unique.

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
        me = ctx.deps.agent_id
        _, tools = await registry.add(name, config, actor=me, owner=me, probe=True)
        return f"registered MCP server {name} with tools: {', '.join(tools) or '(none)'}"

    @ts.tool
    async def mcp_remove(ctx: RunContext[RunScope], name: str) -> str:
        """Remove an MCP server you own (or any, if you are an admin).

        Args:
            name: Server name.
        """
        await registry.remove(name, ctx.deps.agent_id)
        return f"removed MCP server {name}"

    return ts
