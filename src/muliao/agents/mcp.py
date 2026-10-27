"""MCP server 的配置：条目（items，kind=mcp），和 skill、secret 一样按名字全局唯一，每次修改记一版（revisions）。

配置 JSON 存在 items.content（McpConfig），启用与否在 items.enabled。agent 用的 server：它能用的（自己的与分发给它的，
agents.access）、已启用的。

把配置连接成工具是运行时的事（agent_runtime.mcp.McpConnector）：新增时的连接验证由组合根注入（probe），
这里不连接任何 server、不解析 secret。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from muliao.agents.items import changeable_in, creatable_in, existing_in, usable_in
from muliao.agents.kinds import StoredKind, Subject
from muliao.agents.naming import validate_slug
from muliao.agents.revisions import record_in, revision_doc_in
from muliao.agents.tables import Item
from muliao.errors import InvalidInput
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
    """一个已保存的 MCP server。env / headers / args / url 的值可以包含 ${vault:KEY}，按用它的 agent 能用的 secret 解析。"""

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


def server_of(row: Item) -> McpServer:
    # 写入时已按 McpConfig 校验；读回仍按同一条规则解析，不合规则是数据被绕过本模块改坏了
    return McpServer(row.owner, row.name, McpConfig.model_validate_json(row.content), row.enabled)


# 实际连接 server 并列出工具名，${vault:KEY} 按第二个参数（agent 的 id 或 USER）能用的 secret 解析；连不上是
# InvalidInput（把原因交还调用方修正），见 agent_runtime.mcp.McpConnector.probe
McpProbe = Callable[[McpServer, str], Awaitable[list[str]]]


class McpRegistry:
    def __init__(self, db: Database, probe: McpProbe):
        self._db = db
        self._probe = probe

    async def probe(self, server: McpServer, user: str) -> list[str]:
        """实际连接 server 并列出工具名（检验配置能否工作；连不上是 InvalidInput），secret 按 user 能用的解析。"""
        return await self._probe(server, user)

    async def usable(self, user: str) -> list[McpServer]:
        """user（agent 的 id，或 USER 即全部）能用的 server（agents.access，含停用的），按名称。"""
        async with self._db.transaction() as s:
            return [server_of(r) for r in await usable_in(s, StoredKind.MCP, user)]

    async def index(self, agent_id: str) -> list[McpServer]:
        """agent 这次运行要连接的 server：它能用的（agents.access）、已启用的，按名称。"""
        async with self._db.transaction() as s:
            return [server_of(r) for r in await usable_in(s, StoredKind.MCP, agent_id) if r.enabled]

    async def get(self, name: str) -> McpServer:
        async with self._db.transaction() as s:
            return server_of(await existing_in(s, StoredKind.MCP, name))

    async def add(
        self, name: str, config: McpConfig, *, actor: str, owner: str | None, probe: bool
    ) -> tuple[McpServer, list[str]]:
        """新增 server（启用的，归 owner）。probe=True 时先按 actor 能用的 secret 连接验证，验证失败不写入。"""
        validate_slug("mcp server", name)
        server = McpServer(owner=owner, name=name, config=config, enabled=True)
        # 连接验证之前先查一次（正在删除的 agent、已有同名的不必白跑一次连接）；写入时事务里的那次才是准的
        async with self._db.transaction() as s:
            await creatable_in(s, StoredKind.MCP, name, owner)
        tools = await self.probe(server, actor) if probe else []
        async with self._db.transaction() as s:
            await creatable_in(s, StoredKind.MCP, name, owner)
            s.add(Item(kind=StoredKind.MCP.value, name=name, owner=owner, content=config.model_dump_json()))
            record_in(s, Subject.MCP, name, None, document(config, True), actor, f"add MCP server {name}")
        log.info("mcp server added", extra={"owner": owner, "mcp": name, "tools": len(tools)})
        return server, tools

    async def remove(self, name: str, actor: str) -> None:
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.MCP, name, actor)
            server = server_of(row)
            await s.delete(row)
            record_in(
                s,
                Subject.MCP,
                name,
                document(server.config, server.enabled),
                None,
                actor,
                f"remove MCP server {name}",
            )

    async def set_enabled(self, name: str, enabled: bool, actor: str) -> McpServer:
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.MCP, name, actor)
            before = server_of(row)
            row.enabled = enabled
            record_in(
                s,
                Subject.MCP,
                name,
                document(before.config, before.enabled),
                document(before.config, enabled),
                actor,
                f"{'enable' if enabled else 'disable'} MCP server {name}",
            )
            return McpServer(before.owner, name, before.config, enabled)

    async def restore(self, name: str, rev: int, author: str) -> McpServer:
        """把第 rev 版的配置与启用状态作为新的一版写入（不连接验证；归属不变），读版本与写入在同一个事务里。"""
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.MCP, name, author)
            data = json.loads(await revision_doc_in(s, Subject.MCP, name, rev))
            enabled = data.pop("enabled")
            try:
                # JSON 模式：strict 下枚举只从 JSON 文本按值解析
                config = McpConfig.model_validate_json(json.dumps(data))
            except ValidationError as e:
                raise InvalidInput(f"that revision is not a valid MCP config: {e}") from e
            before = server_of(row)
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
