"""挂起的工具调用：模型调用了一个要等别人回信的工具（ask_user 等用户、ask_agent 等另一个 agent），工具调用
agent_runtime 的 defer_call；或工具执行前要用户批准（require_approval，sessions.approvals）。这次运行停在这里正常结束，
不在 worker 里等。回信是一封回答信件（MailKind.ANSWER），
作为那次调用的结果（run_steps 的 deferred）交回，模型看到的就是「我问了，对方这样答」。设计见 docs/architecture.md
「挂起的调用」。

- 三种的差别只写在种类表里（DeferredKind，KINDS），各处查表，不按种类分支。ask_user、ask_agent 按工具名认
  （DEFERRED_TOOLS，工具注册时用这里的名字 `@ts.tool(name=...)`，函数改名不会让挂起判断悄悄失效）；等批准的不按工具名认
  （同一个工具多数调用不用批准），按记录认（sessions.approvals）。一次调用是哪一种只经 kind_of 认。
- 每种的回答信封（回答信件 mail.answers 的值，questions.UserAnswered、answers.AgentAnswered、
  approvals.ApprovalAnswered）带着种类名（tool 字段），自己给出：交给模型的结果（tool_return）、没等到回答就继续时的
  结果（skipped）、调用结束后才到的回答怎么写（late_lines）。
- 哪些调用还挂着由历史推导（pending_calls），只加上交出去的永久事实（HandedOut），不另存状态。
- 什么时候继续（waiting，唯一规则）：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；事件留在信箱里
  等它们一起交给模型。唤醒（mailbox.wake_in）与消费者开头共用这一条。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Annotated, Final, get_args

from pydantic import Field, TypeAdapter
from pydantic_ai.messages import ModelMessage, ToolCallPart, ToolReturn
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.answers import AgentAnswered
from muliao.sessions.approvals import ApprovalAnswered, requested_in
from muliao.sessions.conventions import unanswered_calls
from muliao.sessions.history import since_last_response
from muliao.sessions.holds import called_in
from muliao.sessions.questions import Question, UserAnswered, questions_of
from muliao.sessions.tables import Mail, MailKind

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
    # 用户停止运行时不补「已停止」的结果，仍挂着等回答；回答已提交、还排着的写上那份回答（closing_on_stop）
    kept_open_on_stop: bool
    # 调用里是给用户的提问时，从调用参数取出问题（停下时告诉用户，界面按提问展示），参数不合法时为空；不是提问为 None
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
# 等批准的调用：用户停止时补「已停止」（停止不留着没批准的工具调用）；用户能在界面上批准的就是这一种（open_asks_of）
AWAITING_APPROVAL: Final = DeferredKind(
    answer=ApprovalAnswered, needs_hold=False, replayable=False, kept_open_on_stop=False, questions=None
)
KINDS: Final = (ASK_USER_KIND, ASK_AGENT_KIND, AWAITING_APPROVAL)

# 按工具名认的挂起工具
DEFERRED_TOOLS: Final[Mapping[str, DeferredKind]] = {t.name: t for t in (ASK_USER_KIND, ASK_AGENT_KIND)}


def _deferred_tool(name: str) -> DeferredKind | None:
    return DEFERRED_TOOLS.get(name)


def asks_user(name: str) -> bool:
    """这个工具的调用是给用户的提问（DeferredKind.questions）。"""
    tool = _deferred_tool(name)
    return tool is not None and tool.questions is not None


def questions_in(call: ToolCallPart) -> list[Question]:
    """调用里给用户的问题；不是提问、或参数不合法的调用没有。提问至少有一个问题（questions.Questions），所以非空
    就是提问：要的是问题时只用它判断，不再先问 asks_user。"""
    tool = _deferred_tool(call.tool_name)
    return [] if tool is None or tool.questions is None else tool.questions(call)


@dataclass(frozen=True)
class HandedOut:
    """挂起调用的永久事实（其余由历史推导）：已经交出去的 ask_agent 调用 id（带 call_id 的占用，
    sessions.holds.called_in），与停下来等批准的调用 id（sessions.approvals）。"""

    delegated: AbstractSet[str]
    approvals: AbstractSet[str]


async def handed_out_in(s: AsyncSession, session_id: str) -> HandedOut:
    return HandedOut(await called_in(s, session_id), await requested_in(s, session_id))


def pending_calls(history: Sequence[ModelMessage], handed: HandedOut) -> dict[str, ToolCallPart]:
    """还在等回信的调用，按出现顺序：没有结果的挂起工具调用（参数被拒绝、交还模型重试的有结果，不算）；要占用的
    （ask_agent，DeferredKind.needs_hold）只算已经交出去的。没有占用的 ask_agent 不是在等回信：工具还没执行完
    （回复保存后、投递提交前失败），它是待执行的调用，续跑时再执行（Threads.dispatch 按调用 id 幂等；这一步里
    还有别的没出结果的调用时整步都不重放，见 sessions.consumer.interrupted_calls）。其他工具的调用记过等批准的（
    HandedOut.approvals）在等批准；没记过的是执行到一半被打断的。"""
    return {c.tool_call_id: c for c in unanswered_calls(history) if _is_pending(c, handed)}


def kind_of(call: ToolCallPart, approvals: AbstractSet[str]) -> DeferredKind | None:
    """这次调用若挂起，是哪一种（KINDS）：记过等批准的（approvals，HandedOut.approvals）是等批准，否则按工具名认；
    都不是为 None。两者互斥：挂起的工具请求批准时 consumer.end_of 当场报错，等批准的记录里不会有它们的调用。
    提问的参数不合法（没有问题）时也为 None：这样的调用没执行过（中途打断留下的，或被退回重试的），不是在等用户
    回答，停止与续跑都按普通调用补结果（#563）。"""
    if call.tool_call_id in approvals:
        return AWAITING_APPROVAL
    tool = _deferred_tool(call.tool_name)
    if tool is not None and tool.questions is not None and not tool.questions(call):
        return None
    return tool


def _is_pending(call: ToolCallPart, handed: HandedOut) -> bool:
    kind = kind_of(call, handed.approvals)
    return kind is not None and (not kind.needs_hold or call.tool_call_id in handed.delegated)


def replayable(call: ToolCallPart, handed: HandedOut) -> bool:
    """没挂起、也没结果的这次调用可以原样重新执行（DeferredKind.replayable，按 kind_of 认）。"""
    kind = kind_of(call, handed.approvals)
    return kind is not None and kind.replayable


@dataclass(frozen=True)
class StopClosing:
    """用户停止时怎么关闭没出结果的调用（sessions.halt.close_history_in）：

    - stopped：补「已停止」的结果——停止后不再挂着的调用（DeferredKind.kept_open_on_stop 为假，按 kind_of 认）；
    - answered：停止后本该仍挂着、但回答已提交还排着的调用 -> 交给模型的结果。回答属于它回答的调用，是停止前用户
      已经给出的部分，照常写进历史（同「停止前已经产生的部分照常保存」），不留在信箱里等以后唤醒；
    - used：answered 的回答所在的信件，随之记为已处理（整封信：同一封里对等批准调用的决定也算已处理，那个调用
      已补「已停止」，只影响界面不划掉它，不拆信）。其余仍挂着、没有回答的调用照旧挂着，用户仍可回答。"""

    stopped: list[ToolCallPart]
    answered: list[tuple[ToolCallPart, object]]
    used: list[Mail]


def closing_on_stop(
    history: Sequence[ModelMessage], handed: HandedOut, queued: Sequence[Mail]
) -> StopClosing:
    kept = {
        k: c
        for k, c in pending_calls(history, handed).items()
        if (kind := kind_of(c, handed.approvals)) is not None and kind.kept_open_on_stop
    }
    answers: dict[str, CallResult] = {}
    used: list[Mail] = []
    for m in queued:
        if m.kind != MailKind.ANSWER or not (mine := [k for k in m.answers if k in kept]):
            continue
        used.append(m)
        answers.update({k: call_result(m.answers[k]) for k in mine})
    # 回答怎么变成调用的结果只在 deferred_results 一处定
    results = deferred_results({k: kept[k] for k in answers}, answers, handed)
    for call_id, value in results.items():
        if isinstance(value, ToolReturn):
            # 停止时结果直接写进历史；ToolReturn 要由 pydantic-ai 转换成消息，停止后仍挂着的种类不能交回它
            raise RuntimeError(
                f"call {call_id} of {kept[call_id].tool_name}: a ToolReturn cannot be saved on stop"
            )
    return StopClosing(
        stopped=[c for c in unanswered_calls(history) if c.tool_call_id not in kept],
        answered=[(kept[k], v) for k, v in results.items()],
        used=used,
    )


async def pending_in(s: AsyncSession, session_id: str) -> dict[str, ToolCallPart]:
    """会话现在挂着的调用，从库里的历史推导：只可能在最后一条模型回复里（history.since_last_response），不加载整段
    上下文。"""
    return pending_calls(await since_last_response(s, session_id), await handed_out_in(s, session_id))


def answered_in(queued: Sequence[Mail], pending: Mapping[str, ToolCallPart]) -> dict[str, CallResult]:
    """信箱里对这些挂起调用的回答。回答信件里不针对它们的（迟到的，提问已被别的运行关闭）不在这里，见 is_on_time。"""
    return {
        call_id: call_result(value)
        for m in queued
        if is_on_time(m, pending)
        for call_id, value in m.answers.items()
    }


def is_on_time(mail: Mail, pending: Mapping[str, ToolCallPart]) -> bool:
    """这封回答信件针对的调用都还挂着：作为那些调用的结果交回。否则它是迟到的，作为文字交给模型（late_text）。"""
    return mail.kind == MailKind.ANSWER and set(mail.answers) <= set(pending)


def waiting(history: Sequence[ModelMessage], queued: Sequence[Mail], handed: HandedOut) -> bool:
    """停在挂起的调用上、还不能继续：有挂起的调用，没有新消息，回答也没覆盖全部挂起的调用。"""
    pending = pending_calls(history, handed)
    if not pending or any(m.kind == MailKind.MESSAGE for m in queued):
        return False
    return not set(pending) <= set(answered_in(queued, pending))


def deferred_results(
    pending: Mapping[str, ToolCallPart],
    answers: Mapping[str, CallResult],
    handed: HandedOut,
    *,
    replaced: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """交给 pydantic-ai 的挂起调用结果。有回答的用回答信封的 tool_return；其余（来了新消息，或这次只回答了一部分）用
    那一种信封的 skipped，由它看这次交回的别的回答自己定。

    replaced：另有结果、不用回答的已回答调用（如 sessions.approvals.take_in 给的「被中断」：批准之前已经交给过
    运行去执行，不再执行）。替换哪些、换成什么由那一种自己的模块定，这里只代入。"""
    replaced = replaced or {}
    results: dict[str, object] = {}
    for call_id, call in pending.items():
        kind = kind_of(call, handed.approvals)
        if kind is None:
            raise RuntimeError(f"call {call_id} of {call.tool_name} is not a deferred call")
        result = answers.get(call_id)
        if result is None:
            results[call_id] = kind.answer.skipped(list(answers.values()))
        elif not isinstance(result, kind.answer):
            raise RuntimeError(f"call {call_id} of {call.tool_name} got an answer for {result.tool}")
        elif call_id in replaced:
            results[call_id] = replaced[call_id]
        else:
            results[call_id] = result.tool_return()
    if stray := set(replaced) - (set(pending) & set(answers)):
        raise RuntimeError(f"replaced results for calls that are not answered pending calls: {sorted(stray)}")
    return results


def late_text(results: Sequence[CallResult]) -> str:
    """回答到达时那次调用已经结束（模型已经带着「没回答」或「之后送达」继续了）：作为一段用户消息交给模型，回答不丢。"""
    lines: list[str] = []
    for kind in KINDS:
        lines += kind.answer.late_lines([r for r in results if isinstance(r, kind.answer)])
    return "\n".join(lines)


@dataclass(frozen=True)
class OpenAsks:
    """还没有结果、等用户的挂起调用（提问与等批准的调用）分成两种：现在能回答的（answerable 提问、approvable 等批准的
    调用），与回答已提交、还在信箱里排队的（submitted）。回答在交给模型之前一直排着（运行失败重试时也还在），所以
    不需要别的条件。界面展示与投信校验都经过 open_asks_of（加载在 mailbox.open_asks_in / open_asks_among）。"""

    answerable: dict[str, list[Question]]
    # 现在能批准的调用 id -> 工具名
    approvable: dict[str, str]
    submitted: frozenset[str]
    # 决定已提交、还排着的等批准的调用（submitted 的子集）
    approvals_submitted: frozenset[str]


def open_asks_of(
    since_last_response: Sequence[ModelMessage], queued: Sequence[Mail], requested: AbstractSet[str]
) -> OpenAsks:
    """挂起的调用只可能在最后一条模型回复里（history.since_last_response）；回答已在信箱里排着的算已提交。
    requested 是会话里停下来等过批准的调用（HandedOut.approvals）。哪一种经 kind_of 认：提问是带问题的种类，批准是
    AWAITING_APPROVAL；ask_agent 不等用户，不在其中。"""
    submitted = frozenset(k for m in queued if m.kind == MailKind.ANSWER for k in m.answers)
    questions: dict[str, list[Question]] = {}
    gated: dict[str, str] = {}
    for c in unanswered_calls(since_last_response):
        kind = kind_of(c, requested)
        if kind is not None and kind.questions is not None:
            questions[c.tool_call_id] = kind.questions(c)
        elif kind is AWAITING_APPROVAL:
            gated[c.tool_call_id] = c.tool_name
    return OpenAsks(
        answerable={k: v for k, v in questions.items() if k not in submitted},
        approvable={k: v for k, v in gated.items() if k not in submitted},
        submitted=submitted,
        approvals_submitted=frozenset(k for k in gated if k in submitted),
    )
