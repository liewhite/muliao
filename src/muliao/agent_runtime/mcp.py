"""MCP server 的配置与注册表，以及把配置构建为 pydantic-ai toolset。

配置是资源仓库里的 JSON 文件（public/mcp/<name>.json、agents/<id>/mcp/<name>.json，见 muliao.agents.resources）：
归属与名称由文件位置决定，每次修改提交一版。agent 看到的 server 按 ownership.visible 合并公共与私有。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fastmcp.client.transports import StdioTransport
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_ai import RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, DynamicToolset

from muliao.agents.api import ResourceRepo, scope_of, validate_slug, visible
from muliao.errors import Conflict, CorruptState, InvalidInput, NotFound
from muliao.platform.shell import redact
from muliao.platform.tooling import RunScope
from muliao.vault.api import VAULT_REF, Vault

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0


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


class McpRegistry:
    def __init__(self, repo: ResourceRepo, vault: Vault):
        self._repo = repo
        self._vault = vault

    def _dir(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / "mcp"

    def _path(self, owner: str | None, name: str) -> Path:
        return self._dir(owner) / f"{validate_slug('mcp server', name)}.json"

    def _load(self, owner: str | None, path: Path) -> McpServer:
        """手工改坏的文件在这里报错（带路径），不带着错误的类型或含义继续运行；可以用 remove 删掉。"""
        try:
            validate_slug("mcp server", path.stem)
            config = McpConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValueError, InvalidInput) as e:
            # 文件数据损坏（多半是手工改坏的）：不是调用方的输入错误，交给人修复
            raise CorruptState(f"corrupt MCP config {path}: {e}") from e
        return McpServer(owner=owner, name=path.stem, config=config)

    async def build_toolset(self, server: McpServer) -> AbstractToolset:
        """解析 vault 引用并构建 toolset；工具名以 `<server名>_` 为前缀避免冲突。"""
        c = server.config
        if c.transport == McpTransport.STDIO:
            assert c.command is not None
            client = StdioTransport(
                command=c.command,
                args=[await self._vault.render(a, server.owner) for a in c.args],
                env=await self._vault.render_map(c.env, server.owner) or None,
            )
            toolset = MCPToolset(client, id=f"mcp-{server.key}", init_timeout=_CONNECT_TIMEOUT)
        else:
            assert c.url is not None
            toolset = MCPToolset(
                await self._vault.render(c.url, server.owner),
                id=f"mcp-{server.key}",
                headers=await self._vault.render_map(c.headers, server.owner) or None,
                init_timeout=_CONNECT_TIMEOUT,
            )
        return toolset.prefixed(server.name)

    def toolset(self) -> DynamicToolset[RunScope]:
        """「使用 MCP」：每次运行时按这次运行的 agent（RunScope.agent_id；临时 agent 用调用方的视角）取可见的 server
        构建，与其他工具组一样是一个 toolset，没有可见的 server 时为空。"""

        async def build(ctx: RunContext[RunScope]) -> AbstractToolset[RunScope] | None:
            servers = self.visible_to(ctx.deps.agent_id)
            if not servers:
                return None
            return CombinedToolset([await self.build_toolset(s) for s in servers])

        return DynamicToolset(build, per_run_step=False)

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
            # 异常文本可能带着已代入 secret 的 URL、参数或请求头（如 HTTP 错误里的完整 URL），它会进日志、
            # 交还给模型并返回给调用方：先把这次用到的 secret 明文替换掉（与 run_shell 同一份实现）
            secrets = await self._vault.resolve_many(server.owner, _vault_keys(server.config))
            log.info("mcp probe failed", extra={"mcp": server.name, "error": redact(repr(e), secrets)})
            raise InvalidInput(
                redact(f"could not connect to MCP server {server.name!r}: {type(e).__name__}: {e}", secrets)
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
        self, owner: str | None, name: str, config: McpConfig, *, probe: bool, author: str
    ) -> tuple[McpServer, list[str]]:
        """新增 server。probe=True 时先连接验证，验证失败不写入。owner 对应的 agent 由调用方确认存在。"""
        validate_slug("mcp server", name)
        server = McpServer(owner=owner, name=name, config=config)
        tools = await self.probe(server) if probe else []

        def create(current: str | None) -> str:
            if current is not None:
                raise Conflict(f"MCP server {name!r} already exists in {scope_of(owner)} scope")
            return _dump(config)

        await self._repo.update(
            self._path(owner, name), create, f"add {scope_of(owner)} MCP server {name}", author
        )
        log.info("mcp server added", extra={"owner": owner, "mcp": name, "tools": len(tools)})
        return server, tools

    async def remove(self, owner: str | None, name: str, author: str) -> None:
        """只看文件在不在，不解析内容：改坏的配置也能删掉。"""

        def delete(current: str | None) -> None:
            if current is None:
                raise NotFound(f"MCP server {name!r} not found in {scope_of(owner)} scope")

        await self._repo.update(
            self._path(owner, name), delete, f"remove {scope_of(owner)} MCP server {name}", author
        )

    async def set_enabled(self, owner: str | None, name: str, enabled: bool, author: str) -> McpServer:
        path = self._path(owner, name)
        result: list[McpServer] = []

        def toggle(current: str | None) -> str:
            if current is None:
                raise NotFound(f"MCP server {name!r} not found in {scope_of(owner)} scope")
            server = self._load(owner, path)
            updated = server.config.model_copy(update={"enabled": enabled})
            result.append(McpServer(owner=owner, name=name, config=updated))
            return _dump(updated)

        message = f"{'enable' if enabled else 'disable'} {scope_of(owner)} MCP server {name}"
        await self._repo.update(path, toggle, message, author)
        return result[0]


def _dump(config: McpConfig) -> str:
    return config.model_dump_json(indent=2) + "\n"


def _vault_keys(config: McpConfig) -> list[str]:
    """配置里引用的 vault key（构建 toolset 时代入的就是它们），按出现顺序去重。"""
    templates = [
        *config.args,
        *config.env.values(),
        *([config.url] if config.url else []),
        *config.headers.values(),
    ]
    return list(dict.fromkeys(k for template in templates for k in VAULT_REF.findall(template)))
