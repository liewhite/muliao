"""工具的共同契约（各领域的工具集都依赖它，包括 agent_runtime 之下的 agents，所以它在平台里）：一次运行的
上下文 RunScope 与回答交给谁（Audience），内置工具自己声明的属性（ToolTraits，注册时用 declared 挂在 metadata 上），
以及工具连续失败的容忍次数。会话来源归会话领域（sessions.origins），把领域错误交还模型的包装归 agent_runtime
（tool_errors）。"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from muliao.platform.tool_views import ToolRender


class Audience(StrEnum):
    """一次运行的回答交给谁：运行开始时由会话与它处理的信件推导一次（sessions.consumer），决定运行上下文里怎么说、
    ask_user 调用时接不接受（没有用户会回答就拒绝）。工具列表不随它变（只由分发规则决定，缓存前缀才稳定）。不入库。"""

    # 有用户在等：运行结束时推送
    USER = "user"
    # 委派来的工作（ask_agent、report_back）：回答交回发起委派的 agent
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
    # 这次驱动里 read_file 读过（只读了一段也算，同 Claude Code 的 Write）、或文件工具刚写过的文件：真实路径 → 当时的修改时间（ns）。write_file 整体覆盖资源目录里
    # 已有的文件之前核对它（读过、之后没被别人改过），防止两处同时改时后写的静默盖掉先写的。与 notices 一样每次驱动换新
    reads: dict[Path, int] = field(default_factory=dict, compare=False)

    @property
    def user_waits(self) -> bool:
        """用户在等这次运行的回答：它发起的后台任务、委派的结局随之推送。"""
        return self.audience == Audience.USER


# 工具连续失败的容忍次数；成功一次即清零（pydantic-ai 语义）
TOOL_MAX_RETRIES = 3


class Access(StrEnum):
    """内置工具读还是写（界面、get_agent 展示）。"""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ToolTraits:
    """一个内置工具自己声明的属性：注册时挂在 metadata 上（declared，pydantic-ai 的 metadata 不交给模型），由
    agent_runtime.Toolbox 收集后交给各个用它的地方——分发（读写、管理类）、消息落库前脱敏（secret_args）、界面展示
    （render）。加一个工具只改它自己的定义；每个内置工具都要声明，没声明的启动即报错（Toolbox.traits）。"""

    access: Access
    # 管理类工具（建、删 agent）：内置角色 base 不含，只经 admin 角色或显式规则给出（agents.permissions）
    admin: bool = False
    # 明文参数名：消息落库、推送前替换掉（agent_runtime.redaction）
    secret_args: frozenset[str] = frozenset()
    # 界面上的展示形式（platform.tool_views）；None 显示参数与结果的原文
    render: ToolRender[Any] | None = None


# metadata 里挂 ToolTraits 的键
_TRAITS_KEY = "muliao_tool"


def declared(
    access: Access,
    *,
    admin: bool = False,
    secret_args: Collection[str] = (),
    render: ToolRender[Any] | None = None,
) -> dict[str, Any]:
    """内置工具的 metadata：`@ts.tool(metadata=declared(Access.WRITE, render=...))`。"""
    return {_TRAITS_KEY: ToolTraits(access, admin, frozenset(secret_args), render)}


def traits_in(metadata: Mapping[str, Any] | None) -> ToolTraits | None:
    return None if metadata is None else metadata.get(_TRAITS_KEY)
