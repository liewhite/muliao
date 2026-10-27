"""运行的工具组合：全部工具（静态内置工具集 + 按运行构建的 MCP）与一次运行按 agent 规则开放的那部分（RunProfile）。"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic_ai import FunctionToolset
from pydantic_ai.toolsets import AbstractToolset

from muliao.agent_runtime.tool_results import JsonResults
from muliao.agents.api import AgentInfo, Rules
from muliao.platform.tooling import RunScope


@dataclass(frozen=True)
class RunProfile:
    """一次运行开放的工具：从 Toolbox 里按 agent 的权限规则取。"""

    toolsets: Sequence[AbstractToolset[RunScope]]


@dataclass(frozen=True)
class Toolbox:
    """运行可用的全部工具：静态的内置工具集（登记见 agents.permissions.TOOLS）与按运行构建的 MCP。一次运行开放哪些内置
    工具在运行开始时按 agent 的规则算定（profile），调用时不再判断；MCP 是它能用的、已启用的 server（McpRegistry.index）。"""

    toolsets: Sequence[FunctionToolset[RunScope]]
    # MCP 工具：每次运行新建一个（它带着这次运行的连接状态），按这次运行的 agent 能用的 server 连接
    mcp: Callable[[], AbstractToolset[RunScope]]
    # agent（按 id）现在的规则（Distribution.rules：base 与角色按现在的定义展开）
    rules_of: Callable[[str], Awaitable[Rules]]

    def tool_names(self) -> list[str]:
        return [name for ts in self.toolsets for name in ts.tools]

    async def profile(self, agent: AgentInfo) -> RunProfile:
        allowed = set((await self.rules_of(agent.id)).tools(self.tool_names()))
        # 内置工具的结构化结果交给模型时是一段 JSON 文本（tool_results）；MCP 的结果按 MCP 协议的内容交回，不套
        static: list[AbstractToolset[RunScope]] = [
            JsonResults(ts.filtered(lambda _ctx, td: td.name in allowed))
            for ts in self.toolsets
            if allowed & set(ts.tools)
        ]
        return RunProfile(toolsets=[*static, self.mcp()])
