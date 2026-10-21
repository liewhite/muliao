"""运行给每个工具集套上的错误处理：领域错误交还模型修正（AgentRuntime 组合工具时套上）。"""

from dataclasses import dataclass
from typing import Any

from pydantic_ai import ModelRetry, RunContext, ToolFailed
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from muliao.errors import DomainError
from muliao.platform.tooling import RunScope


@dataclass
class DomainErrorsAsRetry(WrapperToolset[RunScope]):
    """把领域错误（参数不合法、找不到对象等）交还给模型修正，而不是终止整轮运行。

    重试预算用完时不再抛 ModelRetry（pydantic-ai 会以 UnexpectedModelBehavior 终止整轮运行，此前做完的事
    连同回答一起作废），改为 ToolFailed：模型照样看到失败原因，但这次调用到此为止，由它换个做法或放弃这一步。
    整轮运行不设总时长，失控靠用户停止收束。"""

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope]
    ) -> Any:
        try:
            return await super().call_tool(name, tool_args, ctx, tool)
        except DomainError as e:
            if ctx.retry >= tool.max_retries:
                raise ToolFailed(
                    f"{e}\n(`{name}` has now failed {ctx.retry + 1} times in a row; stop retrying the same "
                    "call: change approach, or skip this step and carry on with the rest of the task)"
                ) from e
            raise ModelRetry(str(e)) from e
