from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from muliao.agent_runtime.scope import RunScope
from muliao.errors import DomainError

# 工具连续失败的容忍次数；成功一次即清零（pydantic-ai 语义）
TOOL_MAX_RETRIES = 3


@dataclass
class DomainErrorsAsRetry(WrapperToolset[RunScope]):
    """把领域错误（参数不合法、找不到对象等）交还给模型修正，而不是终止整轮运行。"""

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope]
    ) -> Any:
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        except DomainError as e:
            raise ModelRetry(str(e)) from e
