"""运行的工具组合：全部工具（静态内置工具集 + 按运行构建的 MCP）与一次运行按 agent 规则开放的那部分（RunProfile）。"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic_ai import FunctionToolset
from pydantic_ai.toolsets import AbstractToolset

from muliao.agent_runtime.redaction import Redaction
from muliao.agent_runtime.tool_results import JsonResults
from muliao.agents.api import AgentInfo, Rules, ToolSpec
from muliao.platform.tool_views import BoundRender, ToolRenders
from muliao.platform.tooling import RunScope, ToolTraits, traits_in


@dataclass(frozen=True)
class RunProfile:
    """一次运行开放的工具：从 Toolbox 里按 agent 的权限规则取。"""

    toolsets: Sequence[AbstractToolset[RunScope]]


@dataclass(frozen=True)
class Toolbox:
    """运行可用的全部工具：静态的内置工具集与按运行构建的 MCP。每个内置工具注册时自己声明属性
    （platform.tooling.declared），这里收集成分发用的工具表（specs）、界面的展示规则（renders）与脱敏（redaction）。
    一次运行开放哪些内置工具在运行开始时按 agent 的规则算定（profile），调用时不再判断；MCP 是它能用的、已启用的
    server（McpRegistry.index）。"""

    toolsets: Sequence[FunctionToolset[RunScope]]
    # MCP 工具：每次运行新建一个（它带着这次运行的连接状态），按这次运行的 agent 能用的 server 连接
    mcp: Callable[[], AbstractToolset[RunScope]]
    # agent（按 id）现在的规则（Distribution.rules：base 与角色按现在的定义展开）
    rules_of: Callable[[str], Awaitable[Rules]]

    def __post_init__(self) -> None:
        # 装配时就核对每个内置工具都声明了、没有重名
        self.traits()

    def traits(self) -> dict[str, ToolTraits]:
        """内置工具→它声明的属性，按注册顺序。没声明或重名是代码错误，当场报错。"""
        found: dict[str, ToolTraits] = {}
        for ts in self.toolsets:
            for name, tool in ts.tools.items():
                traits = traits_in(tool.metadata)
                if traits is None:
                    raise RuntimeError(
                        f"built-in tool {name!r} declares no traits (platform.tooling.declared)"
                    )
                if name in found:
                    raise RuntimeError(f"built-in tool {name!r} is registered more than once")
                found[name] = traits
        return found

    def specs(self) -> list[ToolSpec]:
        """分发用的工具表（组合根 bind 给 agents.ToolTable）。"""
        return [ToolSpec(name, t.access, t.admin) for name, t in self.traits().items()]

    def renders(self) -> ToolRenders:
        """内置工具在界面上的展示规则（platform.tool_views）：声明了 render 的那些，配上工具自己的参数校验；
        MCP 工具没有，显示原文。"""
        traits = self.traits()
        return {
            name: BoundRender(name, render, tool.function_schema.validator)
            for ts in self.toolsets
            for name, tool in ts.tools.items()
            if (render := traits[name].render) is not None
        }

    def redaction(self) -> Redaction:
        """消息落库、推送前的脱敏：声明了 secret_args 的那些工具（sessions.steps 用）。"""
        return Redaction({name: t.secret_args for name, t in self.traits().items() if t.secret_args})

    async def profile(self, agent: AgentInfo) -> RunProfile:
        allowed = set((await self.rules_of(agent.id)).tools(self.traits()))
        # 内置工具的结构化结果交给模型时是一段 JSON 文本（tool_results）；MCP 的结果按 MCP 协议的内容交回，不套
        static: list[AbstractToolset[RunScope]] = [
            JsonResults(ts.filtered(lambda _ctx, td: td.name in allowed))
            for ts in self.toolsets
            if allowed & set(ts.tools)
        ]
        return RunProfile(toolsets=[*static, self.mcp()])
