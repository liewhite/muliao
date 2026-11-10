"""MCP server：AI 资源的一种，一个文件（`mcp/<名>.json`，在 owner 的资源目录下，格式见 agents.mcp_config）。agent 用的
server：它能用的（自己的与分发给它的，agents.access）、已启用的。

把配置连接成工具是运行时的事（agent_runtime.mcp.McpConnector）：界面新增时的连接验证由组合根注入（probe），这里不连接
任何 server、不解析 secret。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from muliao.agents import assets
from muliao.agents.access import usable
from muliao.agents.ids import USER
from muliao.agents.kinds import StoredKind, label
from muliao.agents.mcp_config import McpConfig, McpServer, McpTransport, mcp_config, mcp_file, parse_mcp_file
from muliao.agents.source import AssetSource
from muliao.errors import Conflict

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

    async def index(self, agent_id: str) -> list[McpServer]:
        """agent 这次运行要连接的 server：它能用的（agents.access）、已启用的，按名称。"""
        servers = (server_of(a) for a in usable(await self._source.snapshot(), _KIND, agent_id))
        return [s for s in servers if s.enabled]

    async def get(self, name: str) -> McpServer:
        return server_of((await self._source.snapshot()).asset(_KIND, name))

    async def add(
        self, name: str, config: McpConfig, *, owner: str | None, probe: bool
    ) -> tuple[McpServer, list[str]]:
        """用户在界面上新增 server（启用的，归 owner）。probe=True 时先连接验证（secret 按用户的——全部——解析），
        验证失败不写入。"""
        server = McpServer(owner=owner, name=name, config=config, enabled=True)
        # 连接验证之前先查一次（已有同名的不必白跑一次连接）；写入口按此刻的树再查（asset_files.vet）
        snapshot = await self._source.snapshot()
        path = snapshot.path_of(_KIND, name, owner)
        snapshot.check_free(_KIND, name)
        tools = await self.probe(server, USER) if probe else []

        def new(path: Path) -> None:
            if path.exists():
                raise Conflict(
                    f"{label(_KIND)} {name!r} already exists; names are unique, choose another name"
                )

        await self._source.write({path: mcp_file(config, True)}, new)
        log.info("mcp server added", extra={"owner": owner, "mcp": name, "tools": len(tools)})
        return server, tools

    async def remove(self, name: str) -> None:
        async with self._source.writing() as snapshot:
            asset = snapshot.asset(_KIND, name)
            await asyncio.to_thread(assets.remove, asset.path)

    async def set_enabled(self, name: str, enabled: bool) -> McpServer:
        asset = (await self._source.snapshot()).asset(_KIND, name)
        server = server_of(asset)

        def unchanged(path: Path) -> None:
            # 只改启用状态：期间配置被别处改过、删掉的不盖掉
            if not path.is_file() or parse_mcp_file(path.read_text(encoding="utf-8"))[0] != server.config:
                raise Conflict(f"{label(_KIND)} {name!r} was changed elsewhere meanwhile; reload and retry")

        await self._source.write({asset.path: mcp_file(server.config, enabled)}, unchanged)
        return McpServer(server.owner, name, server.config, enabled)
