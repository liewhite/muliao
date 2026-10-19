"""MCP server 配置的注册表，以及把配置构建为 pydantic-ai toolset。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastmcp.client.transports import StdioTransport
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from muliao.db import Database
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.models import Agent, McpServer, McpTransport
from muliao.naming import validate_slug
from muliao.ownership import scope_of
from muliao.vault import Vault

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0


@dataclass(frozen=True)
class McpSpec:
    name: str
    transport: McpTransport
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()

    def validate(self) -> None:
        validate_slug("mcp server", self.name)
        if self.transport == McpTransport.STDIO:
            if not self.command or self.url or self.headers:
                raise InvalidInput("stdio MCP server requires 'command' and must not set 'url' or 'headers'")
        elif self.transport == McpTransport.HTTP:
            if not self.url or self.command or self.args or self.env:
                raise InvalidInput(
                    "http MCP server requires 'url' and must not set 'command', 'args' or 'env'"
                )
            if not self.url.startswith(("http://", "https://")):
                raise InvalidInput("MCP url must start with http:// or https://")
        else:
            raise InvalidInput(f"unknown MCP transport {self.transport!r}")


class McpRegistry:
    def __init__(self, db: Database, vault: Vault):
        self._db = db
        self._vault = vault

    async def build_toolset(self, server: McpServer) -> AbstractToolset:
        """解析 vault 引用并构建 toolset；工具名以 `<server名>_` 为前缀避免冲突。"""
        if server.transport == McpTransport.STDIO:
            assert server.command is not None
            client = StdioTransport(
                command=server.command,
                args=[await self._vault.render(a) for a in server.args],
                env=await self._vault.render_map(server.env) or None,
            )
            toolset = MCPToolset(client, id=f"mcp-{server.id}", init_timeout=_CONNECT_TIMEOUT)
        else:
            assert server.url is not None
            toolset = MCPToolset(
                await self._vault.render(server.url),
                id=f"mcp-{server.id}",
                headers=await self._vault.render_map(server.headers) or None,
                init_timeout=_CONNECT_TIMEOUT,
            )
        return toolset.prefixed(server.name)

    async def probe(self, server: McpServer) -> list[str]:
        """实际连接 server 并列出工具名；连接失败原样抛出。"""
        toolset = await self.build_toolset(server)
        inner = toolset.wrapped
        assert isinstance(inner, MCPToolset)
        async with inner:
            return [t.name for t in await inner.list_tools()]

    async def visible_to(self, agent_name: str) -> list[McpServer]:
        """agent 可用的 server：公共 + 自己私有，且已启用。私有与公共同名时私有生效。"""
        async with self._db.transaction() as s:
            rows = (
                await s.scalars(
                    select(McpServer)
                    .where(McpServer.enabled, or_(McpServer.owner.is_(None), McpServer.owner == agent_name))
                    .order_by(McpServer.name)
                )
            ).all()
        private = {r.name for r in rows if r.owner is not None}
        return [r for r in rows if r.owner is not None or r.name not in private]

    async def list(self, owner: str | None, *, all_owners: bool = False) -> list[McpServer]:
        query = select(McpServer).order_by(McpServer.owner, McpServer.name)
        if not all_owners:
            query = query.where(McpServer.owner.is_(None) if owner is None else McpServer.owner == owner)
        async with self._db.transaction() as s:
            return list((await s.scalars(query)).all())

    def _row(self, owner: str | None, spec: McpSpec) -> McpServer:
        spec.validate()
        return McpServer(
            owner=owner,
            name=spec.name,
            transport=spec.transport,
            command=spec.command,
            args=list(spec.args),
            url=spec.url,
            env=dict(spec.env),
            headers=dict(spec.headers),
            enabled=True,
        )

    async def add(self, owner: str | None, spec: McpSpec, *, probe: bool) -> tuple[McpServer, list[str]]:
        """新增 server。probe=True 时先连接验证，验证失败不写入。"""
        row = self._row(owner, spec)
        tools = await self.probe(row) if probe else []
        try:
            async with self._db.transaction() as s:
                if owner is not None and await s.get(Agent, owner) is None:
                    raise NotFound(f"agent {owner!r} not found")
                s.add(row)
        except IntegrityError as e:
            raise Conflict(f"MCP server {spec.name!r} already exists in {scope_of(owner)} scope") from e
        log.info("mcp server added", extra={"owner": owner, "mcp": spec.name, "tools": len(tools)})
        return row, tools

    async def _find(self, s, owner: str | None, name: str) -> McpServer:  # noqa: ANN001
        row = await s.scalar(
            select(McpServer).where(
                McpServer.name == name,
                McpServer.owner.is_(None) if owner is None else McpServer.owner == owner,
            )
        )
        if row is None:
            raise NotFound(f"MCP server {name!r} not found in {scope_of(owner)} scope")
        return row

    async def get(self, owner: str | None, name: str) -> McpServer:
        async with self._db.transaction() as s:
            return await self._find(s, owner, name)

    async def remove(self, owner: str | None, name: str) -> None:
        async with self._db.transaction() as s:
            await s.delete(await self._find(s, owner, name))

    async def set_enabled(self, owner: str | None, name: str, enabled: bool) -> McpServer:
        async with self._db.transaction() as s:
            row = await self._find(s, owner, name)
            row.enabled = enabled
            return row
