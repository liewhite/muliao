"""把 MCP 配置变成一次运行的工具（「使用 MCP」组）与连接验证。配置本身是 agent 领域的资源（agents.mcp.McpRegistry）。

一个 server 出问题只影响它自己：配置文件坏了（CorruptResource）、引用的 secret 不在了、连不上，这次运行就没有它的
工具，其余 server 照常可用（列工具失败同样只跳过它）；原因记日志，并作为 RunScope.notices 放进这次运行开头的运行上下文告诉模型。
运行中调用工具失败只是这次调用失败（ToolFailed 交还模型），断开失败只记 warning。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from fastmcp.client.transports import StdioTransport
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred, ModelRetry, ToolFailed
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset, DynamicToolset, ToolsetTool, WrapperToolset

from muliao.agent_runtime.transient import is_transient
from muliao.agents.api import (
    CorruptResource,
    McpConfig,
    McpServer,
    McpTransport,
)
from muliao.errors import InvalidInput, NotFound, TransientError
from muliao.platform.shell import redact
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.vault.api import VAULT_REF, Vault, fill

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT = 30.0

# agent 此刻可用的 server（它私有的与勾选了的公共的），以及配置坏了的（agents.mcp.McpRegistry.index）
McpIndex = Callable[[str, Sequence[str]], tuple[list[McpServer], list[CorruptResource]]]


class McpFailed(Exception):
    """MCP server 连接或通信失败。信息已脱敏，原始异常不挂在后面（见 _SecretSafe）。"""


# 工具调用的正常结局（延后执行、等待批准），不是 server 故障，原样放行。交还模型的错误只脱敏信息（文本来自 server
# 或网关）：ToolFailed 保留类型；ModelRetry（server 拒绝这次调用，MCPToolset 默认如此报）换成 InvalidInput，由
# DomainErrorsAsRetry 统一管重试预算，连续拒绝不终止整轮运行（#145）
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
    TransientError，照常由任务队列重试。调用工具的非暂时性失败是 ToolFailed（交还模型），断开失败只记 warning。secrets 与代入配置的是同一份明文（build_toolset 只解析一次）。"""

    server: str
    secrets: Mapping[str, str]

    @contextmanager
    def guard(self, failed: type[Exception] = McpFailed) -> Iterator[None]:
        """非暂时性失败换成 `failed`（默认 McpFailed；调用工具时是 ToolFailed，交还模型）。"""
        try:
            yield
        except _TOOL_OUTCOMES:
            raise
        except ModelRetry as e:
            raise InvalidInput(redact(e.message, self.secrets)) from None
        except ToolFailed as e:
            raise ToolFailed(redact(e.message, self.secrets)) from None
        except Exception as e:
            message = redact(f"MCP server {self.server!r} failed: {_describe(e)}", self.secrets)
            raise (TransientError if is_transient(e) else failed)(message) from None

    async def __aenter__(self) -> Self:
        with self.guard():
            await self.wrapped.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        """断开失败只记 warning：工具都用完了，不让已经做完的运行（或探测）因此失败。"""
        try:
            with self.guard():
                return await self.wrapped.__aexit__(*args)
        except (McpFailed, TransientError) as e:
            # toolset id 带归属（mcp-<范围>:<owner>:<名称>），同名的私有 server 分得开
            log.warning("mcp server disconnect failed", extra={"mcp": self.wrapped.id, "error": str(e)})
            return None

    async def get_tools(self, ctx: RunContext[RunScope]) -> dict[str, ToolsetTool[RunScope]]:
        with self.guard():
            return await self.wrapped.get_tools(ctx)

    async def list_tools(self) -> list[str]:
        """连接期间列出 server 的工具名；MCPToolset 会缓存结果，此后的 get_tools 直接用缓存。"""
        inner = self.wrapped
        assert isinstance(inner, MCPToolset)
        with self.guard():
            return [t.name for t in await inner.list_tools()]

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope]
    ) -> Any:
        # server 在调用中出错只是这次调用失败，交还模型（暂时性失败仍由任务队列重试）
        with self.guard(ToolFailed):
            return await self.wrapped.call_tool(name, tool_args, ctx, tool)


@dataclass
class _Reachable(CombinedToolset[RunScope]):
    """一次运行用到的 server：逐个连接并列一次工具，连不上或列不出工具的（McpFailed、TransientError，信息已
    脱敏）跳过并记进 notices，其余照常组合。工具在进入时就列好（MCPToolset 缓存它），之后每步的 get_tools 用缓存，
    notices 也赶得上这次运行开头的运行上下文。运行中 server 通知工具变了（tools/list_changed 清掉缓存）、重新列又
    失败时，那一步照常失败（少见，不另设一条半路跳过的路径）。暂时性失败也是跳过（这次运行
    没有它的工具），不让整次运行失败重试。只进入一次（每次运行由 toolset() 新建一个）。"""

    # 与 toolsets 一一对应的 server 名，以及这次运行的 agent：记日志用
    servers: list[str] = field(default_factory=list)
    agent: str = ""
    notices: list[str] = field(default_factory=list)

    async def __aenter__(self) -> Self:
        reachable: list[AbstractToolset[RunScope]] = []
        async with AsyncExitStack() as stack:
            for name, toolset in zip(self.servers, self.toolsets, strict=True):
                try:
                    # 一个 server 一个栈：列工具失败时只断开它自己
                    async with AsyncExitStack() as one:
                        await one.enter_async_context(toolset)
                        await _secret_safe(toolset).list_tools()
                        await stack.enter_async_context(one.pop_all())
                except (McpFailed, TransientError) as e:
                    _skip(self.notices, agent=self.agent, server=name, reason=str(e))
                    continue
                reachable.append(toolset)
            self._exit_stack = stack.pop_all()
        self.toolsets = reachable
        return self


def _secret_safe(toolset: AbstractToolset[RunScope]) -> _SecretSafe:
    """build_toolset 构建的 toolset（带前缀）里的 _SecretSafe。"""
    assert isinstance(toolset, WrapperToolset)
    safe = toolset.wrapped
    assert isinstance(safe, _SecretSafe)
    return safe


def _skip(notices: list[str], *, agent: str, server: str, reason: str) -> None:
    """这次运行跳过一个 server：记日志，并告诉模型（notices）。reason 已脱敏。"""
    log.warning("mcp server unavailable", extra={"agent": agent, "mcp": server, "error": reason})
    notices.append(f"An MCP server is unavailable this run, so its tools are missing: {reason}")


class McpConnector:
    """按配置连接 MCP server：解析 vault 引用、构建 toolset、脱敏失败信息。"""

    def __init__(self, vault: Vault):
        self._vault = vault

    async def build_toolset(self, server: McpServer, cwd: Path | None) -> AbstractToolset:
        """解析 vault 引用并构建 toolset；工具名以 `<server名>_` 为前缀避免冲突。cwd：stdio server 进程的工作目录——运行时是
        这次运行的工作目录，server 写出的文件（如浏览器的截图、页面快照）落在 agent 的文件工具与 shell 读得到的地方；
        新增时的探测没有运行，为 None（沿用服务进程的）。"""
        c = server.config
        # 只解析一次：代入配置的与脱敏用的是同一份明文
        secrets = await self._vault.resolve_many(server.owner, _vault_keys(c))
        if c.transport == McpTransport.STDIO:
            assert c.command is not None
            client = StdioTransport(
                command=c.command,
                args=[fill(a, secrets) for a in c.args],
                env={k: fill(v, secrets) for k, v in c.env.items()} or None,
                cwd=None if cwd is None else str(cwd),
            )
            toolset = MCPToolset(
                client, id=f"mcp-{server.key}", init_timeout=_CONNECT_TIMEOUT, max_retries=TOOL_MAX_RETRIES
            )
        else:
            assert c.url is not None
            toolset = MCPToolset(
                fill(c.url, secrets),
                id=f"mcp-{server.key}",
                headers={k: fill(v, secrets) for k, v in c.headers.items()} or None,
                init_timeout=_CONNECT_TIMEOUT,
                max_retries=TOOL_MAX_RETRIES,
            )
        return _SecretSafe(toolset, server=server.name, secrets=secrets).prefixed(server.name)

    def toolset(self, index: McpIndex, selected: Sequence[str]) -> DynamicToolset[RunScope]:
        """使用 MCP：每次运行时按这次运行的 agent（RunScope.agent_id；临时 agent 用调用方的视角）取可用的 server
        （index：它私有的与勾选了的公共 server，selected）构建，是一个 toolset，没有可用的 server 时为空。"""

        async def build(ctx: RunContext[RunScope]) -> AbstractToolset[RunScope] | None:
            agent, notices = ctx.deps.agent_id, ctx.deps.notices
            servers, corrupt = index(agent, selected)
            notices += [c.describe() for c in corrupt]
            names: list[str] = []
            toolsets: list[AbstractToolset[RunScope]] = []
            for server in servers:
                try:
                    toolsets.append(await self.build_toolset(server, ctx.deps.workspace))
                except NotFound as e:
                    # 配置引用的 secret 不在了（vault.resolve_many）
                    _skip(notices, agent=agent, server=server.name, reason=f"MCP server {server.name!r}: {e}")
                    continue
                names.append(server.name)
            return _Reachable(toolsets, servers=names, agent=agent, notices=notices) if toolsets else None

        return DynamicToolset(build, per_run_step=False)

    async def probe(self, server: McpServer) -> list[str]:
        """实际连接 server 并列出工具名。

        探测的目的就是检验用户/agent 给出的配置能否工作，连接失败是预期结果之一：
        各种传输层异常（进程退出、HTTP 错误、协议错误）统一转为 InvalidInput，把原因交还给调用方修正。
        vault 引用缺失在构建阶段以 NotFound 报出，不在此转换。
        """
        safe = _secret_safe(await self.build_toolset(server, None))
        try:
            # 与运行期同一个连接、列工具与脱敏入口
            async with safe:
                return await safe.list_tools()
        except (McpFailed, TransientError) as e:
            log.info("mcp probe failed", extra={"mcp": server.name, "error": str(e)})
            raise InvalidInput(f"could not connect: {e}") from None


def _vault_keys(config: McpConfig) -> list[str]:
    """配置里引用的 vault key（构建 toolset 时代入的就是它们），按出现顺序去重。"""
    templates = [
        *config.args,
        *config.env.values(),
        *([config.url] if config.url else []),
        *config.headers.values(),
    ]
    return list(dict.fromkeys(k for template in templates for k in VAULT_REF.findall(template)))
