"""MCP server 的配置：条目（items，kind=mcp），和 skill、secret 一样按名字全局唯一，每次修改记一版（revisions）。

配置 JSON 存在 items.content（McpConfig），启用与否在 items.enabled。agent 用的 server：它自己的（已启用），加上它勾选了的
（AgentInfo.mcp_servers）、已启用的公共 server（#235 改为按角色分发之前的过渡规则）。

把配置连接成工具是运行时的事（agent_runtime.mcp.McpConnector）：新增时的连接验证由组合根注入（probe），
这里不连接任何 server、不解析 secret。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from muliao.agents.items import item_in, items_in, owned_item_in, writable_in
from muliao.agents.naming import validate_slug
from muliao.agents.ownership import owned_by, scope_of, visible_owners
from muliao.agents.revisions import Subject, record_in
from muliao.agents.tables import Item
from muliao.errors import Conflict, InvalidInput, NotFound
from muliao.platform.db import Database

log = logging.getLogger(__name__)


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class McpConfig(BaseModel):
    """一个 MCP server 的连接配置，也是 items.content 里存的 JSON。传输方式与字段的搭配在这里校验，写入与读回同一条规则。
    字段全部必填：存下的总是写全；调用方输入里可以省略的字段由 mcp_config 补上。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    transport: McpTransport
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]

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
    """调用方（用户、agent）给出的新配置，可以省略与传输方式无关的字段。不合法是输入错误。"""
    fields = {
        "transport": transport,
        "command": command,
        "args": args or [],
        "url": url,
        "env": env or {},
        "headers": headers or {},
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
    enabled: bool

    @property
    def transport(self) -> McpTransport:
        return self.config.transport


def document(config: McpConfig, enabled: bool) -> str:
    """一个 server 在版本表里的文档：配置与启用状态。"""
    return json.dumps({**config.model_dump(mode="json"), "enabled": enabled}, indent=1)


def _server(row: Item) -> McpServer:
    # 写入时已按 McpConfig 校验；读回仍按同一条规则解析，不合规则是数据被绕过本模块改坏了
    return McpServer(row.owner, row.name, McpConfig.model_validate_json(row.content), row.enabled)


# 实际连接 server 并列出工具名；连不上是 InvalidInput（把原因交还调用方修正），见 agent_runtime.mcp.McpConnector.probe
McpProbe = Callable[[McpServer], Awaitable[list[str]]]


class McpRegistry:
    def __init__(self, db: Database, probe: McpProbe):
        self._db = db
        self._probe = probe

    async def probe(self, server: McpServer) -> list[str]:
        """实际连接 server 并列出工具名（检验配置能否工作；连不上是 InvalidInput）。"""
        return await self._probe(server)

    async def list(self, owner: str | None) -> list[McpServer]:
        """owner 名下的 server（None 为公共），按名称。"""
        async with self._db.transaction() as s:
            return [_server(r) for r in await items_in(s, Subject.MCP, owned_by(Item.owner, owner))]

    async def view(self, owner: str | None) -> list[McpServer]:
        """管理界面：owner 视角下的 server，即它私有的（在前）与公共的；owner 为 None 时只有公共 server。"""
        async with self._db.transaction() as s:
            return [_server(r) for r in await items_in(s, Subject.MCP, visible_owners(Item.owner, owner))]

    async def listing(self, agent_id: str, selected: Collection[str]) -> list[tuple[McpServer, bool]]:
        """agent 视角下的全部 server（它私有的在前，然后公共的，含停用的与没勾选的），各自标明是否生效：已启用，且是它
        私有的或它勾选了的公共的（selected，AgentInfo.mcp_servers）。"""
        servers = await self.view(agent_id)
        return [(s, s.enabled and (s.owner is not None or s.name in selected)) for s in servers]

    async def index(self, agent_id: str, selected: Collection[str]) -> list[McpServer]:
        """agent 可用的 server（listing 里生效的）。"""
        return [s for s, active in await self.listing(agent_id, selected) if active]

    async def get(self, owner: str | None, name: str) -> McpServer:
        async with self._db.transaction() as s:
            return _server(await owned_item_in(s, Subject.MCP, owner, name))

    async def add(
        self, owner: str | None, name: str, config: McpConfig, *, probe: bool, author: str
    ) -> tuple[McpServer, list[str]]:
        """新增 server（启用的）。probe=True 时先连接验证，验证失败不写入。"""
        validate_slug("mcp server", name)
        server = McpServer(owner=owner, name=name, config=config, enabled=True)
        # 连接验证之前先查一次（正在删除的 agent、已有同名的不必白跑一次连接）；写入时事务里的那次才是准的
        await self._check_new(owner, name)
        tools = await self.probe(server) if probe else []
        async with self._db.transaction() as s:
            await self._check_new_in(s, owner, name)
            s.add(Item(kind=Subject.MCP.value, name=name, owner=owner, content=config.model_dump_json()))
            record_in(
                s,
                Subject.MCP,
                name,
                None,
                document(config, True),
                author,
                f"add {scope_of(owner)} MCP server {name}",
            )
        log.info("mcp server added", extra={"owner": owner, "mcp": name, "tools": len(tools)})
        return server, tools

    async def _check_new(self, owner: str | None, name: str) -> None:
        async with self._db.transaction() as s:
            await self._check_new_in(s, owner, name)

    @staticmethod
    async def _check_new_in(s, owner: str | None, name: str) -> None:  # noqa: ANN001
        if await writable_in(s, Subject.MCP, owner, name) is not None:
            raise Conflict(f"MCP server {name!r} already exists in {scope_of(owner)} scope")

    async def remove(self, owner: str | None, name: str, author: str) -> None:
        async with self._db.transaction() as s:
            row = await owned_item_in(s, Subject.MCP, owner, name, write=True)
            server = _server(row)
            await s.delete(row)
            record_in(
                s,
                Subject.MCP,
                name,
                document(server.config, server.enabled),
                None,
                author,
                f"remove {scope_of(owner)} MCP server {name}",
            )

    async def set_enabled(self, owner: str | None, name: str, enabled: bool, author: str) -> McpServer:
        async with self._db.transaction() as s:
            row = await owned_item_in(s, Subject.MCP, owner, name, write=True)
            before = _server(row)
            row.enabled = enabled
            record_in(
                s,
                Subject.MCP,
                name,
                document(before.config, before.enabled),
                document(before.config, enabled),
                author,
                f"{'enable' if enabled else 'disable'} {scope_of(owner)} MCP server {name}",
            )
            return McpServer(owner, name, before.config, enabled)

    async def restore(self, name: str, doc: str, author: str) -> McpServer:
        """把某一版的配置与启用状态作为新的一版写入（不连接验证；归属不变）。"""
        data = json.loads(doc)
        enabled = data.pop("enabled")
        try:
            # JSON 模式：strict 下枚举只从 JSON 文本按值解析
            config = McpConfig.model_validate_json(json.dumps(data))
        except ValidationError as e:
            raise InvalidInput(f"that revision is not a valid MCP config: {e}") from e
        async with self._db.transaction() as s:
            row = await item_in(s, Subject.MCP, name)
            if row is None:
                raise NotFound(f"MCP server {name!r} not found; restoring a removed server is not supported")
            before = _server(row)
            row.content, row.enabled = config.model_dump_json(), enabled
            record_in(
                s,
                Subject.MCP,
                name,
                document(before.config, before.enabled),
                document(config, enabled),
                author,
                f"restore MCP server {name}",
            )
            return McpServer(row.owner, name, config, enabled)
