"""MCP server 的配置：带版本的资源，和 skill、memory、指令一样归 agent 领域。

配置是资源仓库里的 JSON 文件（public/mcp/<name>.json、agents/<id>/mcp/<name>.json，见 muliao.agents.resources）：
归属与名称由文件位置决定，每次修改提交一版。agent 看到的 server 按 ownership.shadowing 合并公共与私有。

把配置连接成工具是运行时的事（agent_runtime.mcp.McpConnector）：新增时的连接验证由组合根注入（probe），
这里不连接任何 server、不解析 secret。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from muliao.agents.active import OwnerCheck
from muliao.agents.naming import validate_slug
from muliao.agents.ownership import scope_of, shadowing
from muliao.agents.resources import CorruptResource, ResourceRepo
from muliao.errors import Conflict, CorruptState, InvalidInput, NotFound

log = logging.getLogger(__name__)


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class McpConfig(BaseModel):
    """一个 MCP server 的配置，也是文件里存的内容：除归属与名称以外的字段（这两项由文件位置决定，不存第二份）。
    传输方式与字段的搭配在这里校验，写入与读回同一条规则。字段全部必填：存下的文件总是写全（_dump），缺字段就是
    被改坏了；调用方输入里可以省略的字段由 mcp_config 补上。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    transport: McpTransport
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    enabled: bool

    @model_validator(mode="after")
    def _fits_transport(self) -> McpConfig:
        if self.transport == McpTransport.STDIO:
            if not self.command or self.url or self.headers:
                raise ValueError("stdio MCP server requires 'command' and must not set 'url' or 'headers'")
        elif not self.url or self.command or self.args or self.env:
            raise ValueError("http MCP server requires 'url' and must not set 'command', 'args' or 'env'")
        elif not self.url.startswith(("http://", "https://")):
            raise ValueError("MCP url must start with http:// or https://")
        return self


def mcp_config(
    *,
    transport: McpTransport,
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> McpConfig:
    """调用方（用户、agent）给出的新配置，可以省略与传输方式无关的字段；新增的 server 是启用的。不合法是输入错误。"""
    fields = {
        "transport": transport,
        "command": command,
        "args": args or [],
        "url": url,
        "env": env or {},
        "headers": headers or {},
        "enabled": True,
    }
    try:
        return McpConfig.model_validate(fields)
    except ValidationError as e:
        raise InvalidInput(f"invalid MCP server config: {e}") from e


@dataclass(frozen=True)
class McpServer:
    """一个已保存的 MCP server。env / headers / args / url 的值可以包含 ${vault:KEY}，运行时按归属者的视角解析。"""

    owner: str | None
    name: str
    config: McpConfig

    @property
    def transport(self) -> McpTransport:
        return self.config.transport

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    @property
    def key(self) -> str:
        """在所有 server 里唯一：归属 + 名称。"""
        return f"{scope_of(self.owner)}:{self.owner or ''}:{self.name}"


# 实际连接 server 并列出工具名；连不上是 InvalidInput（把原因交还调用方修正），见 agent_runtime.mcp.McpConnector.probe
McpProbe = Callable[[McpServer], Awaitable[list[str]]]


class McpRegistry:
    def __init__(self, repo: ResourceRepo, probe: McpProbe, check_owner: OwnerCheck):
        self._repo = repo
        # 写私有配置之前在仓库的锁里检查 owner 还可用（agents.active）
        self._check_owner = check_owner
        self._probe = probe

    def _dir(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / "mcp"

    def _path(self, owner: str | None, name: str) -> Path:
        return self._dir(owner) / f"{validate_slug('mcp server', name)}.json"

    def _load(self, owner: str | None, path: Path) -> McpServer:
        """手工改坏的文件在这里报错（带路径），不带着错误的类型或含义继续运行；可以用 remove 删掉。列出 server 时
        它单独报出（_scan），不连累别的 server。"""
        try:
            validate_slug("mcp server", path.stem)
            config = McpConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, InvalidInput) as e:
            # 文件数据损坏（多半是手工改坏的）：不是调用方的输入错误，交给人修复
            raise CorruptState(f"corrupt MCP config {path}: {e}") from e
        return McpServer(owner=owner, name=path.stem, config=config)

    async def probe(self, server: McpServer) -> list[str]:
        """实际连接 server 并列出工具名（检验配置能否工作；连不上是 InvalidInput）。"""
        return await self._probe(server)

    def _scan(self, owner: str | None) -> tuple[list[McpServer], list[CorruptResource]]:
        """owner 名下的 server：读得出来的，和配置坏了的（CorruptResource，每次读到都记 error）。"""
        directory = self._dir(owner)
        if not directory.exists():
            return [], []
        servers: list[McpServer] = []
        corrupt: list[CorruptResource] = []
        for path in sorted(directory.glob("*.json")):
            try:
                servers.append(self._load(owner, path))
            except CorruptState as e:
                log.error("corrupt mcp config", extra={"owner": owner, "mcp": path.stem, "error": str(e)})
                corrupt.append(CorruptResource("MCP server", owner, path.stem, path, str(e)))
        return servers, corrupt

    def list(self, owner: str | None) -> list[McpServer]:
        """owner 名下配置读得出来的 server；坏的见 corrupt_in。"""
        return self._scan(owner)[0]

    def corrupt_in(self, owner: str | None) -> list[CorruptResource]:
        """owner 名下配置坏了的 server（管理界面与 mcp_list 列出，供删除或重写）。"""
        return self._scan(owner)[1]

    def view(self, owner: str | None) -> list[tuple[McpServer, bool]]:
        """管理界面：owner 视角下的 server，即它私有的（在前）与公共的，各自标明是否被同名私有 server 遮蔽。

        停用的 server 不参与合并（不遮蔽别人）；owner 为 None 时只有公共 server。
        """
        return _with_shadowing((self.list(owner) if owner is not None else []) + self.list(None), owner)

    def index(self, agent_id: str) -> tuple[list[McpServer], list[CorruptResource]]:
        """agent 可用的 server（view 里已启用且未被遮蔽的），以及它私有的与公共的里配置坏了的。坏的不参与同名遮蔽
        （是否启用都读不出来）。每个目录只扫一次。"""
        private, private_corrupt = self._scan(agent_id)
        public, public_corrupt = self._scan(None)
        usable = [
            s for s, shadowed in _with_shadowing(private + public, agent_id) if s.enabled and not shadowed
        ]
        return usable, private_corrupt + public_corrupt

    def visible_to(self, agent_id: str) -> list[McpServer]:
        return self.index(agent_id)[0]

    def get(self, owner: str | None, name: str) -> McpServer:
        path = self._path(owner, name)
        if not path.exists():
            raise NotFound(f"MCP server {name!r} not found in {scope_of(owner)} scope")
        return self._load(owner, path)

    async def add(
        self, owner: str | None, name: str, config: McpConfig, *, probe: bool, author: str
    ) -> tuple[McpServer, list[str]]:
        """新增 server。probe=True 时先连接验证，验证失败不写入。"""
        validate_slug("mcp server", name)
        server = McpServer(owner=owner, name=name, config=config)
        tools = await self.probe(server) if probe else []

        def create(current: str | None) -> str:
            if current is not None:
                raise Conflict(f"MCP server {name!r} already exists in {scope_of(owner)} scope")
            return _dump(config)

        await self._repo.update(
            self._path(owner, name),
            create,
            f"add {scope_of(owner)} MCP server {name}",
            author,
            precondition=partial(self._check_owner, owner),
        )
        log.info("mcp server added", extra={"owner": owner, "mcp": name, "tools": len(tools)})
        return server, tools

    async def remove(self, owner: str | None, name: str, author: str) -> None:
        """只看文件在不在，不解析内容：改坏的配置也能删掉。"""

        def delete(current: str | None) -> None:
            if current is None:
                raise NotFound(f"MCP server {name!r} not found in {scope_of(owner)} scope")

        await self._repo.update(
            self._path(owner, name),
            delete,
            f"remove {scope_of(owner)} MCP server {name}",
            author,
            precondition=partial(self._check_owner, owner),
        )

    async def set_enabled(self, owner: str | None, name: str, enabled: bool, author: str) -> McpServer:
        path = self._path(owner, name)
        result: list[McpServer] = []

        def toggle(current: str | None) -> str:
            if current is None:
                raise NotFound(f"MCP server {name!r} not found in {scope_of(owner)} scope")
            try:
                server = self._load(owner, path)
            except CorruptState as e:
                # 交还调用方（不让模型这次运行失败）：坏的配置只能删掉重加
                raise InvalidInput(f"{e}; remove it and add it again") from e
            updated = server.config.model_copy(update={"enabled": enabled})
            result.append(McpServer(owner=owner, name=name, config=updated))
            return _dump(updated)

        message = f"{'enable' if enabled else 'disable'} {scope_of(owner)} MCP server {name}"
        await self._repo.update(path, toggle, message, author, precondition=partial(self._check_owner, owner))
        return result[0]


def _with_shadowing(servers: list[McpServer], owner: str | None) -> list[tuple[McpServer, bool]]:
    """servers 各自标明是否被同名私有 server 遮蔽：停用的不参与合并（不遮蔽别人）。"""
    enabled = [s for s in servers if s.enabled]
    shadowed = {
        id(s)
        for s, hit in shadowing(enabled, agent_id=owner, owner=lambda s: s.owner, name=lambda s: s.name)
        if hit
    }
    return [(s, id(s) in shadowed) for s in servers]


def _dump(config: McpConfig) -> str:
    return config.model_dump_json(indent=2) + "\n"
