"""MCP server 的配置与注册表，以及把配置构建为 pydantic-ai toolset。

配置是资源仓库里的 JSON 文件（public/mcp/<name>.json、agents/<id>/mcp/<name>.json，见 muliao.agents.resources）：
归属与名称由文件位置决定，每次修改提交一版。agent 看到的 server 按 ownership.shadowing 合并公共与私有。

一个 server 出问题只影响它自己：配置文件坏了（CorruptResource）、引用的 secret 不在了、连不上，这次运行就没有它的
工具，其余 server 照常可用；原因记日志，并作为 RunScope.notices 放进这次运行开头的运行上下文告诉模型。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from fastmcp.client.transports import StdioTransport
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, ToolFailed
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, DynamicToolset, ToolsetTool, WrapperToolset

from muliao.agents.api import CorruptResource, ResourceRepo, scope_of, shadowing, validate_slug
from muliao.errors import Conflict, CorruptState, InvalidInput, NotFound, TransientError
from muliao.platform.shell import redact
from muliao.platform.tooling import RunScope
from muliao.platform.worker import is_transient
from muliao.vault.api import VAULT_REF, Vault, fill

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


class McpFailed(Exception):
    """MCP server 连接或通信失败。信息已脱敏，原始异常不挂在后面（见 _SecretSafe）。"""


# 工具调用的正常结局（延后执行、等待批准），不是 server 故障，原样放行；交还模型的错误（ModelRetry、ToolFailed）
# 保留类型、只脱敏信息（文本来自 server 或网关）
_TOOL_OUTCOMES = (CallDeferred, ApprovalRequired)


def _describe(e: BaseException) -> str:
    """异常的类型与信息；异常组展开每个子异常（组本身的 str 只有「n sub-exceptions」）。"""
    if isinstance(e, BaseExceptionGroup):
        return f"{type(e).__name__}: {e.message} [{'; '.join(_describe(x) for x in e.exceptions)}]"
    return f"{type(e).__name__}: {e}"


@dataclass
class _SecretSafe(WrapperToolset[RunScope]):
    """MCP 的连接、列工具、调用工具、断开失败时，异常文本可能带着已代入 secret 的 URL、参数或请求头（如 HTTP 错误
    里的完整 URL），它会进 tasks.error、服务日志（带 traceback）并交还给调用方。运行期与 probe 都经 guard：换成信息
    里 secret 明文已替换为 [secret:KEY] 的异常（与 run_shell 同一份 redact），不带原始异常链；暂时性失败仍是
    TransientError，照常由任务队列重试。secrets 与代入配置的是同一份明文（build_toolset 只解析一次）。"""

    server: str
    secrets: Mapping[str, str]

    @contextmanager
    def guard(self) -> Iterator[None]:
        try:
            yield
        except _TOOL_OUTCOMES:
            raise
        except (ModelRetry, ToolFailed) as e:
            raise type(e)(redact(e.message, self.secrets)) from None
        except Exception as e:
            message = redact(f"MCP server {self.server!r} failed: {_describe(e)}", self.secrets)
            raise (TransientError if is_transient(e) else McpFailed)(message) from None

    async def __aenter__(self) -> Self:
        with self.guard():
            await self.wrapped.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        with self.guard():
            return await self.wrapped.__aexit__(*args)

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        with self.guard():
            return await self.wrapped.get_tools(ctx)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope]
    ) -> Any:
        with self.guard():
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)


@dataclass
class _Reachable(CombinedToolset[RunScope]):
    """一次运行用到的 server：逐个连接，连不上的（McpFailed、TransientError，信息已脱敏）跳过并记进 notices，
    其余照常组合。"""

    notices: list[str] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        reachable: list[AbstractToolset[RunScope]] = []
        async with AsyncExitStack() as stack:
            for toolset in self.toolsets:
                try:
                    await stack.enter_async_context(toolset)
                except (McpFailed, TransientError) as e:
                    log.warning("mcp server unavailable", extra={"error": str(e)})
                    self.notices.append(_unavailable(str(e)))
                    continue
                reachable.append(toolset)
            self._exit_stack = stack.pop_all()
        self.toolsets = reachable
        return self


def _unavailable(reason: str) -> str:
    return f"An MCP server is unavailable this run, so its tools are missing: {reason}"


# 「使用 MCP」组的工具：每次运行按 agent 可见的 server 构建，工具名带 server 前缀（给模型看的工具组说明用它）
MCP_TOOL_NAMES = "<server>_<tool> for every tool of the MCP servers the agent can see"


class McpRegistry:
    def __init__(self, repo: ResourceRepo, vault: Vault):
        self._repo = repo
        self._vault = vault

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

    async def build_toolset(self, server: McpServer) -> AbstractToolset:
        """解析 vault 引用并构建 toolset；工具名以 `<server名>_` 为前缀避免冲突。"""
        c = server.config
        # 只解析一次：代入配置的与脱敏用的是同一份明文
        secrets = await self._vault.resolve_many(server.owner, _vault_keys(c))
        if c.transport == McpTransport.STDIO:
            assert c.command is not None
            client = StdioTransport(
                command=c.command,
                args=[fill(a, secrets) for a in c.args],
                env={k: fill(v, secrets) for k, v in c.env.items()} or None,
            )
            toolset = MCPToolset(client, id=f"mcp-{server.key}", init_timeout=_CONNECT_TIMEOUT)
        else:
            assert c.url is not None
            toolset = MCPToolset(
                fill(c.url, secrets),
                id=f"mcp-{server.key}",
                headers={k: fill(v, secrets) for k, v in c.headers.items()} or None,
                init_timeout=_CONNECT_TIMEOUT,
            )
        return _SecretSafe(toolset, server=server.name, secrets=secrets).prefixed(server.name)

    def toolset(self) -> DynamicToolset[RunScope]:
        """「使用 MCP」：每次运行时按这次运行的 agent（RunScope.agent_id；临时 agent 用调用方的视角）取可见的 server
        构建，与其他工具组一样是一个 toolset，没有可见的 server 时为空。"""

        async def build(ctx: RunContext[RunScope]) -> AbstractToolset[RunScope] | None:
            agent, notices = ctx.deps.agent_id, ctx.deps.notices
            notices += [c.describe() for c in self.corrupt(agent)]
            toolsets: list[AbstractToolset[RunScope]] = []
            for server in self.visible_to(agent):
                try:
                    toolsets.append(await self.build_toolset(server))
                except NotFound as e:
                    # 配置引用的 secret 不在了（vault.resolve_many）
                    log.warning("mcp server unavailable", extra={"mcp": server.name, "error": str(e)})
                    notices.append(_unavailable(f"MCP server {server.name!r}: {e}"))
            return _Reachable(toolsets, notices=notices) if toolsets else None

        return DynamicToolset(build, per_run_step=False)

    async def probe(self, server: McpServer) -> list[str]:
        """实际连接 server 并列出工具名。

        探测的目的就是检验用户/agent 给出的配置能否工作，连接失败是预期结果之一：
        各种传输层异常（进程退出、HTTP 错误、协议错误）统一转为 InvalidInput，把原因交还给调用方修正。
        vault 引用缺失在构建阶段以 NotFound 报出，不在此转换。
        """
        safe = (await self.build_toolset(server)).wrapped
        assert isinstance(safe, _SecretSafe)
        inner = safe.wrapped
        assert isinstance(inner, MCPToolset)
        try:
            # 与运行期同一个脱敏入口
            with safe.guard():
                async with inner:
                    return [t.name for t in await inner.list_tools()]
        except (McpFailed, TransientError) as e:
            log.info("mcp probe failed", extra={"mcp": server.name, "error": str(e)})
            raise InvalidInput(f"could not connect: {e}") from None

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
        """owner 名下配置读得出来的 server；坏的见 corrupt。"""
        return self._scan(owner)[0]

    def corrupt(self, agent_id: str) -> list[CorruptResource]:
        """agent 看得到的（它私有的与公共的）配置坏了的 server。它们不参与同名遮蔽（是否启用都读不出来）。"""
        return self._scan(agent_id)[1] + self._scan(None)[1]

    def view(self, owner: str | None) -> list[tuple[McpServer, bool]]:
        """管理界面：owner 视角下的 server，即它私有的（在前）与公共的，各自标明是否被同名私有 server 遮蔽。

        停用的 server 不参与合并（不遮蔽别人）；owner 为 None 时只有公共 server。
        """
        servers = (self.list(owner) if owner is not None else []) + self.list(None)
        enabled = [s for s in servers if s.enabled]
        shadowed = {
            id(s)
            for s, hit in shadowing(enabled, agent_id=owner, owner=lambda s: s.owner, name=lambda s: s.name)
            if hit
        }
        return [(s, id(s) in shadowed) for s in servers]

    def visible_to(self, agent_id: str) -> list[McpServer]:
        """agent 可用的 server：view 里已启用且未被遮蔽的。"""
        return [s for s, shadowed in self.view(agent_id) if s.enabled and not shadowed]

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
