"""工具的共同契约（各领域的工具集都依赖它，它只依赖平台）：一次运行的上下文 RunScope 与它的词汇（会话来源、回答交给谁），
工具连续失败的容忍次数，以及把领域错误交还模型修正的包装。"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic_ai import ModelRetry, RunContext, ToolFailed
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from muliao.errors import DomainError


class Origin(StrEnum):
    """会话来源：用户在网页发起、触发器（定时、外部事件……）、agent 之间委派、issue 看板唤醒被指派的 agent。"""

    USER = "user"
    TRIGGER = "trigger"
    DELEGATION = "delegation"
    ISSUE = "issue"


class Audience(StrEnum):
    """一次运行的回答交给谁：运行开始时由会话与它处理的信件推导一次（sessions.consumer），决定开放哪些工具（ask_user）
    与指令里怎么说。不入库。"""

    # 有用户在等：运行结束时推送
    USER = "user"
    # 私有 thread：回答作为 ask_agent 的结果交回发问方
    AGENT = "agent"
    # 没有人在等（触发器、运行中检查……）：要通知用户时 agent 自己 notify_user
    NONE = "none"


@dataclass(frozen=True)
class RunScope:
    """一次 agent 运行的上下文，作为 pydantic-ai 的 deps 传给所有工具。"""

    agent_id: str
    workspace: Path
    # 反思与临时 agent 不属于任何会话，以下三项均为 None
    session_id: str | None
    origin: Origin | None
    audience: Audience | None

    @property
    def user_waits(self) -> bool:
        """用户在等这次运行的回答：它发起的后台任务、委派的结局随之推送。"""
        return self.audience == Audience.USER


# 工具连续失败的容忍次数；成功一次即清零（pydantic-ai 语义）
TOOL_MAX_RETRIES = 3


@dataclass
class DomainErrorsAsRetry(WrapperToolset[RunScope]):
    """把领域错误（参数不合法、找不到对象等）交还给模型修正，而不是终止整轮运行。

    重试预算用完时不再抛 ModelRetry（pydantic-ai 会以 UnexpectedModelBehavior 终止整轮运行，此前做完的事
    连同回答一起作废），改为 ToolFailed：模型照样看到失败原因，但这次调用到此为止，由它换个做法或放弃这一步。
    整轮运行的失控由运行级的请求上限约束。"""

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
