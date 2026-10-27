"""工具的共同契约（各领域的工具集都依赖它，包括 agent_runtime 之下的 agents、vault，所以它在平台里）：一次运行的
上下文 RunScope 与回答交给谁（Audience），以及工具连续失败的容忍次数。会话来源归会话领域（sessions.origins），
把领域错误交还模型的包装归 agent_runtime（tool_errors）。"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Audience(StrEnum):
    """一次运行的回答交给谁：运行开始时由会话与它处理的信件推导一次（sessions.consumer），决定运行上下文里怎么说、
    ask_user 调用时接不接受（没有用户会回答就拒绝）。工具列表不随它变（只由分发规则决定，缓存前缀才稳定）。不入库。"""

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
    session_id: str
    audience: Audience
    # 运行开始时工具组发现、要告诉模型的问题（如这次连不上的 MCP server）：工具组构建、连接时追加，运行把它们放进
    # 开头交给模型的运行上下文（AgentRuntime）。每次驱动一份：只由 AgentRuntime 在驱动前换新（dataclasses.replace），
    # 别处构造的 RunScope 不要和别的运行共用这个列表
    notices: list[str] = field(default_factory=list, compare=False)

    @property
    def user_waits(self) -> bool:
        """用户在等这次运行的回答：它发起的后台任务、委派的结局随之推送。"""
        return self.audience == Audience.USER


# 工具连续失败的容忍次数；成功一次即清零（pydantic-ai 语义）
TOOL_MAX_RETRIES = 3
