"""挂起调用的三种（ask_user 等用户回答、ask_agent 等另一个 agent、等用户批准）各自的规则：一张表，各处查表，不按
种类分支。挂起、唤醒与交回的流程见 sessions.pending，设计见 docs/architecture.md「挂起的调用」。

前两种按工具名认（DEFERRED_TOOLS，工具注册时用这里的名字 `@ts.tool(name=...)`，函数改名不会让挂起判断悄悄失效）；
等批准的不按工具名认（同一个工具多数调用不用批准），按记录认（sessions.approvals，见 pending.kind_of）。

每种的回答信封（回答信件 mail.answers 的值）带着种类名（tool 字段），自己给出：交给模型的结果（tool_return）、
没等到回答就继续时的结果（skipped）、调用结束后才到的回答怎么写（late_lines）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, Field, TypeAdapter
from pydantic_ai import ToolReturn
from pydantic_ai.messages import ToolCallPart

from muliao.agent_runtime.api import Attachment
from muliao.sessions.answers import Answer
from muliao.sessions.approvals import ApprovalAnswered
from muliao.sessions.questions import Question, UserAnswered, questions_of

ASK_AGENT: Final = "ask_agent"

# ask_agent 答复之前来了新消息：这次调用不再等，模型知道回答之后会作为事件送达
STILL_WORKING = (
    "A new message arrived before the agent you asked answered. Its answer will reach you as an event "
    "in this conversation when it is ready."
)


class AgentAnswer(BaseModel):
    """被问的 agent 交回的结局（sessions.delegation）：给发问方模型的文字，它挂给用户的文件。做它的 thread 不在这里：
    调用开出了哪个 thread 记在 sessions.thread_calls。更早投出的回答信件里的 thread 键忽略。"""

    text: str
    attachments: list[Attachment]

    def to_tool_return(self) -> ToolReturn:
        return Answer(self.text, list(self.attachments)).to_tool_return()


class AgentAnswered(BaseModel):
    """回答信件里一次 ask_agent 调用的结果：被问的 agent 的结局。"""

    tool: Literal["ask_agent"]
    result: AgentAnswer

    @classmethod
    def of(cls, result: AgentAnswer) -> AgentAnswered:
        return cls(tool="ask_agent", result=result)

    def tool_return(self) -> object:
        return self.result.to_tool_return()

    @staticmethod
    def skipped(answered_other: bool) -> object:
        return STILL_WORKING

    @staticmethod
    def late_lines(answers: Sequence[AgentAnswered]) -> list[str]:
        return [f"An agent you asked earlier answered:\n{a.result.text}" for a in answers]


# 回答信件里一次调用的结果：信封的 tool 是种类名（等批准的是 approval），按它区分。成员与 KINDS 的信封一致（测试守住）
CallResult = Annotated[UserAnswered | AgentAnswered | ApprovalAnswered, Field(discriminator="tool")]
_CALL_RESULT: TypeAdapter[CallResult] = TypeAdapter(CallResult)


def call_result(value: object) -> CallResult:
    """读回信件里存的结果；不合法是数据与代码不一致。"""
    return _CALL_RESULT.validate_python(value)


@dataclass(frozen=True)
class DeferredKind:
    # 回答信封；种类名（name）就是它 tool 字段的取值
    answer: type[UserAnswered] | type[AgentAnswered] | type[ApprovalAnswered]
    # 交出去时留下带调用 id 的占用（sessions.holds），有占用才算挂起；没有占用的调用是工具还没执行完
    needs_hold: bool
    # 没有挂起、也没有结果的调用可以原样重新执行（按调用 id 幂等，没有别的副作用），续跑时不补「被中断」；
    # 只有要占用的工具才会有没挂起的调用
    replayable: bool
    # 用户停止运行时不补「已停止」的结果，仍挂着等回答
    kept_open_on_stop: bool
    # 调用里是给用户的提问时，从调用参数取出问题（停下时告诉用户，界面按提问展示）；不是提问为 None
    questions: Callable[[ToolCallPart], list[Question]] | None

    def __post_init__(self) -> None:
        if self.replayable and not self.needs_hold:
            raise ValueError(f"{self.name}: only tools that need a hold can have calls that are not pending")

    @property
    def name(self) -> str:
        (name,) = get_args(self.answer.model_fields["tool"].annotation)
        return name


ASK_USER_KIND: Final = DeferredKind(
    answer=UserAnswered, needs_hold=False, replayable=False, kept_open_on_stop=True, questions=questions_of
)
ASK_AGENT_KIND: Final = DeferredKind(
    answer=AgentAnswered, needs_hold=True, replayable=True, kept_open_on_stop=False, questions=None
)
# 等批准的调用：可能要批准的工具是普通工具，其余规则同普通工具：被打断不重放，用户停止时补「已停止」
AWAITING_APPROVAL: Final = DeferredKind(
    answer=ApprovalAnswered, needs_hold=False, replayable=False, kept_open_on_stop=False, questions=None
)
KINDS: Final = (ASK_USER_KIND, ASK_AGENT_KIND, AWAITING_APPROVAL)

# 按工具名认的挂起工具
DEFERRED_TOOLS: Final[Mapping[str, DeferredKind]] = {t.name: t for t in (ASK_USER_KIND, ASK_AGENT_KIND)}


def deferred_tool(name: str) -> DeferredKind | None:
    """这个工具的挂起规则；不是挂起的工具为 None。"""
    return DEFERRED_TOOLS.get(name)


def _flag(name: str, pick: Callable[[DeferredKind], bool]) -> bool:
    tool = deferred_tool(name)
    return tool is not None and pick(tool)


def asks_user(name: str) -> bool:
    """这个工具的调用是给用户的提问（DeferredKind.questions）。"""
    return _flag(name, lambda t: t.questions is not None)


def questions_in(call: ToolCallPart) -> list[Question]:
    """调用里给用户的问题；不是提问的调用（包括参数被退回的提问）没有。提问至少有一个问题（questions.Questions），
    所以非空就是提问：要的是问题时只用它判断，不再先问 asks_user。"""
    tool = deferred_tool(call.tool_name)
    return [] if tool is None or tool.questions is None else tool.questions(call)


def replayable(name: str) -> bool:
    """没挂起、也没结果的这个工具的调用可以原样重新执行（DeferredKind.replayable）。"""
    return _flag(name, lambda t: t.replayable)


def kept_open_on_stop(name: str) -> bool:
    """用户停止运行时这个工具的调用仍挂着（DeferredKind.kept_open_on_stop）。"""
    return _flag(name, lambda t: t.kept_open_on_stop)
