"""挂起的工具（ask_user、ask_agent）各自的规则：一张表，各处查表，不按工具名分支。挂起、唤醒与交回的流程见
sessions.pending，设计见 docs/architecture.md「挂起的调用」。工具注册时用这里的名字（`@ts.tool(name=...)`），
函数改名不会让挂起判断悄悄失效。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from pydantic_ai.messages import ToolCallPart

from muliao.sessions.questions import ASK_USER, NOT_ANSWERED, SKIPPED, Question, questions_of

ASK_AGENT: Final = "ask_agent"

# ask_agent 答复之前来了新消息：这次调用不再等，模型知道回答之后会作为事件送达
STILL_WORKING = (
    "A new message arrived before the agent you asked answered. Its answer will reach you as an event "
    "in this conversation when it is ready."
)


@dataclass(frozen=True)
class DeferredTool:
    name: str
    # 交出去时留下带调用 id 的占用（sessions.holds），有占用才算挂起；没有占用的调用是工具还没执行完
    needs_hold: bool
    # 没有挂起、也没有结果的调用可以原样重新执行（按调用 id 幂等，没有别的副作用），续跑时不补「被中断」；
    # 只有要占用的工具才会有没挂起的调用
    replayable: bool
    # 用户停止运行时不补「已停止」的结果，仍挂着等回答
    kept_open_on_stop: bool
    # 调用里是给用户的提问时，从调用参数取出问题（停下时告诉用户，界面按提问展示）；参数不合法时抛出
    # ValidationError / KeyError。不是提问为 None
    questions: Callable[[ToolCallPart], list[Question]] | None
    # 没等到回答就继续时（来了新消息，或这次只回答了别的调用）交给模型的结果；参数：这次是否回答了某个提问
    skipped_result: Callable[[bool], object]

    def __post_init__(self) -> None:
        if self.replayable and not self.needs_hold:
            raise ValueError(f"{self.name}: only tools that need a hold can have calls that are not pending")


def _user_skipped(answered_other: bool) -> object:
    return (SKIPPED if answered_other else NOT_ANSWERED).model_dump()


DEFERRED_TOOLS: Final[Mapping[str, DeferredTool]] = {
    t.name: t
    for t in (
        DeferredTool(
            name=ASK_USER,
            needs_hold=False,
            replayable=False,
            kept_open_on_stop=True,
            questions=questions_of,
            skipped_result=_user_skipped,
        ),
        DeferredTool(
            name=ASK_AGENT,
            needs_hold=True,
            replayable=True,
            kept_open_on_stop=False,
            questions=None,
            skipped_result=lambda _: STILL_WORKING,
        ),
    )
}


def deferred_tool(name: str) -> DeferredTool | None:
    """这个工具的挂起规则；不是挂起的工具为 None。"""
    return DEFERRED_TOOLS.get(name)


def _flag(name: str, pick: Callable[[DeferredTool], bool]) -> bool:
    tool = deferred_tool(name)
    return tool is not None and pick(tool)


def asks_user(name: str) -> bool:
    """这个工具的调用是给用户的提问（DeferredTool.questions）。"""
    return _flag(name, lambda t: t.questions is not None)


def questions_in(call: ToolCallPart) -> list[Question]:
    """调用里给用户的问题；不是提问的调用没有。"""
    tool = deferred_tool(call.tool_name)
    return [] if tool is None or tool.questions is None else tool.questions(call)


def replayable(name: str) -> bool:
    """没挂起、也没结果的这个工具的调用可以原样重新执行（DeferredTool.replayable）。"""
    return _flag(name, lambda t: t.replayable)


def kept_open_on_stop(name: str) -> bool:
    """用户停止运行时这个工具的调用仍挂着（DeferredTool.kept_open_on_stop）。"""
    return _flag(name, lambda t: t.kept_open_on_stop)
