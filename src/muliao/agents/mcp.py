"""MCP server：AI 资源的一种，一个文件（`mcp/<名>.json`，在 owner 的资源目录下，格式见 agents.mcp_config）。agent 用的
server：它能用的（自己的与分发给它的，agents.access）、已启用的。

把配置连接成工具是运行时的事（agent_runtime.mcp.McpConnector）：界面新增时的连接验证由组合根注入（probe），这里不连接
任何 server、不解析 secret。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from muliao.agents import assets
from muliao.agents.access import changeable, usable
from muliao.agents.kinds import StoredKind, label
from muliao.agents.mcp_config import McpConfig, McpServer, McpTransport, mcp_config, mcp_file
from muliao.agents.naming import validate_slug
from muliao.agents.source import AssetSource
from muliao.platform.fs import write_atomic

log = logging.getLogger(__name__)

__all__ = ["McpConfig", "McpRegistry", "McpServer", "McpTransport", "mcp_config", "server_of"]

_KIND = StoredKind.MCP


def server_of(a: assets.Asset) -> McpServer:
    assert a.mcp is not None, "an MCP asset always carries its config"
    config, enabled = a.mcp
    return McpServer(a.owner, a.name, config, enabled)


# 实际连接 server 并列出工具名，${vault:KEY} 按第二个参数（agent 的 id 或 USER）能用的 secret 解析；连不上是
# InvalidInput（把原因交还调用方修正），见 agent_runtime.mcp.McpConnector.probe
McpProbe = Callable[[McpServer, str], Awaitable[list[str]]]


class McpRegistry:
    def __init__(self, source: AssetSource, probe: McpProbe):
        self._source = source
        self._probe = probe

    async def probe(self, server: McpServer, user: str) -> list[str]:
        """实际连接 server 并列出工具名（检验配置能否工作；连不上是 InvalidInput），secret 按 user 能用的解析。"""
        return await self._probe(server, user)

    async def usable(self, user: str) -> list[McpServer]:
        """user（agent 的 id，或 USER 即全部）能用的 server（agents.access，含停用的），按名称。"""
        return [server_of(a) for a in usable(await self._source.snapshot(), _KIND, user)]

    async def index(self, agent_id: str) -> list[McpServer]:
        """agent 这次运行要连接的 server：它能用的（agents.access）、已启用的，按名称。"""
        return [s for s in await self.usable(agent_id) if s.enabled]

    async def get(self, name: str) -> McpServer:
        return server_of((await self._source.snapshot()).asset(_KIND, name))

    async def add(
        self, name: str, config: McpConfig, *, actor: str, owner: str | None, probe: bool
    ) -> tuple[McpServer, list[str]]:
        """新增 server（启用的，归 owner）。probe=True 时先按 actor 能用的 secret 连接验证，验证失败不写入。"""
        validate_slug(label(_KIND), name)
        server = McpServer(owner=owner, name=name, config=config, enabled=True)
        # 连接验证之前先查一次（已有同名的不必白跑一次连接）；写入时那次才是准的
        self._check_free((await self._source.snapshot()), name)
        tools = await self.probe(server, actor) if probe else []
        async with self._source.writing() as snapshot:
            self._check_free(snapshot, name)
            path = snapshot.path_of(_KIND, name, owner)
            await asyncio.to_thread(write_atomic, path, mcp_file(config, True))
        log.info("mcp server added", extra={"owner": owner, "mcp": name, "tools": len(tools)})
        return server, tools

    @staticmethod
    def _check_free(snapshot: assets.Snapshot, name: str) -> None:
        snapshot.check_free(_KIND, name)

    async def remove(self, name: str, actor: str) -> None:
        async with self._source.writing() as snapshot:
            asset = changeable(snapshot, _KIND, name, actor)
            await asyncio.to_thread(assets.remove, asset.path)

    async def set_enabled(self, name: str, enabled: bool, actor: str) -> McpServer:
        async with self._source.writing() as snapshot:
            server = server_of(changeable(snapshot, _KIND, name, actor))
            path = snapshot.asset(_KIND, name).path
            await asyncio.to_thread(write_atomic, path, mcp_file(server.config, enabled))
            return McpServer(server.owner, name, server.config, enabled)
