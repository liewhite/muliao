"""MCP server 的配置与注册表，以及把配置构建为 pydantic-ai toolset。

配置是资源仓库里的 JSON 文件（public/mcp/<name>.json、agents/<id>/mcp/<name>.json，见 muliao.resources）：
归属与名称由文件位置决定，每次修改提交一版。agent 看到的 server 按 ownership.visible 合并公共与私有。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from fastmcp.client.transports import StdioTransport
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset

from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.naming import validate_slug
from muliao.ownership import scope_of, visible
from muliao.resources import ResourceRepo
from muliao.vault import Vault

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


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


@dataclass(frozen=True)
class McpServer:
    """一个已保存的 MCP server。env / headers / args / url 的值可以包含 ${vault:KEY}，运行时按归属者的视角解析。"""

    owner: str | None
    name: str
    transport: McpTransport
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    enabled: bool

    @property
    def key(self) -> str:
        """在所有 server 里唯一：归属 + 名称。"""
        return f"{scope_of(self.owner)}:{self.owner or ''}:{self.name}"


# 文件里存除归属与名称以外的字段（这两项由文件位置决定，不存第二份）
_FILE_FIELDS = ("transport", "command", "args", "url", "env", "headers", "enabled")


class McpRegistry:
    def __init__(self, repo: ResourceRepo, vault: Vault):
        self._repo = repo
        self._vault = vault

    def _dir(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / "mcp"

    def _path(self, owner: str | None, name: str) -> Path:
        return self._dir(owner) / f"{validate_slug('mcp server', name)}.json"

    def _load(self, owner: str | None, path: Path) -> McpServer:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if set(data) != set(_FILE_FIELDS):
            raise RuntimeError(f"{path}: MCP config must have exactly the fields {list(_FILE_FIELDS)}")
        return McpServer(
            owner=owner, name=path.stem, **{**data, "transport": McpTransport(data["transport"])}
        )

    @staticmethod
    def _dump(server: McpServer) -> str:
        data = {k: v for k, v in asdict(server).items() if k in _FILE_FIELDS}
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    async def build_toolset(self, server: McpServer) -> AbstractToolset:
        """解析 vault 引用并构建 toolset；工具名以 `<server名>_` 为前缀避免冲突。"""
        if server.transport == McpTransport.STDIO:
            assert server.command is not None
            client = StdioTransport(
                command=server.command,
                args=[await self._vault.render(a, server.owner) for a in server.args],
                env=await self._vault.render_map(server.env, server.owner) or None,
            )
            toolset = MCPToolset(client, id=f"mcp-{server.key}", init_timeout=_CONNECT_TIMEOUT)
        else:
            assert server.url is not None
            toolset = MCPToolset(
                await self._vault.render(server.url, server.owner),
                id=f"mcp-{server.key}",
                headers=await self._vault.render_map(server.headers, server.owner) or None,
                init_timeout=_CONNECT_TIMEOUT,
            )
        return toolset.prefixed(server.name)

    async def probe(self, server: McpServer) -> list[str]:
        """实际连接 server 并列出工具名。

        探测的目的就是检验用户/agent 给出的配置能否工作，连接失败是预期结果之一：
        各种传输层异常（进程退出、HTTP 错误、协议错误）统一转为 InvalidInput，把原因交还给调用方修正。
        vault 引用缺失在构建阶段以 NotFound 报出，不在此转换。
        """
        toolset = await self.build_toolset(server)
        inner = toolset.wrapped
        assert isinstance(inner, MCPToolset)
        try:
            async with inner:
                return [t.name for t in await inner.list_tools()]
        except Exception as e:
            log.info("mcp probe failed", extra={"mcp": server.name, "error": repr(e)})
            raise InvalidInput(
                f"could not connect to MCP server {server.name!r}: {type(e).__name__}: {e}"
            ) from e

    def list(self, owner: str | None) -> list[McpServer]:
        directory = self._dir(owner)
        if not directory.exists():
            return []
        return [self._load(owner, p) for p in sorted(directory.glob("*.json"))]

    def list_all(self) -> list[McpServer]:
        """管理界面：公共与所有 agent 私有的 server。"""
        return [*self.list(None), *(s for owner in self._repo.agent_owners() for s in self.list(owner))]

    def visible_to(self, agent_id: str) -> list[McpServer]:
        """agent 可用的 server：已启用的，按 ownership.visible 合并公共与私有。"""
        enabled = [s for s in self.list(agent_id) + self.list(None) if s.enabled]
        return visible(enabled, agent_id=agent_id, owner=lambda s: s.owner, name=lambda s: s.name)

    def get(self, owner: str | None, name: str) -> McpServer:
        path = self._path(owner, name)
        if not path.exists():
            raise NotFound(f"MCP server {name!r} not found in {scope_of(owner)} scope")
        return self._load(owner, path)

    async def add(
        self, owner: str | None, spec: McpSpec, *, probe: bool, author: str
    ) -> tuple[McpServer, list[str]]:
        """新增 server。probe=True 时先连接验证，验证失败不写入。"""
        spec.validate()
        if owner is not None and not self._repo.owner_dir(owner).exists():
            raise NotFound(f"agent {owner!r} not found")
        path = self._path(owner, spec.name)
        if path.exists():
            raise Conflict(f"MCP server {spec.name!r} already exists in {scope_of(owner)} scope")
        server = McpServer(
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
        tools = await self.probe(server) if probe else []
        await self._repo.apply(
            {path: self._dump(server)}, f"add {scope_of(owner)} MCP server {spec.name}", author
        )
        log.info("mcp server added", extra={"owner": owner, "mcp": spec.name, "tools": len(tools)})
        return server, tools

    async def remove(self, owner: str | None, name: str, author: str) -> None:
        self.get(owner, name)
        await self._repo.apply(
            {self._path(owner, name): None}, f"remove {scope_of(owner)} MCP server {name}", author
        )

    async def set_enabled(self, owner: str | None, name: str, enabled: bool, author: str) -> McpServer:
        server = self.get(owner, name)
        updated = McpServer(**{**asdict(server), "enabled": enabled})
        message = f"{'enable' if enabled else 'disable'} {scope_of(owner)} MCP server {name}"
        await self._repo.apply({self._path(owner, name): self._dump(updated)}, message, author)
        return updated
