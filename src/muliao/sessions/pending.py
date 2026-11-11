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
- 会话停在哪（position，纯函数，唯一的判断）：空闲、新开、从历史续跑、被打断、等回答。由历史、交出去的永久事实
  （HandedOut）与信箱推导，不另存状态；消费者、唤醒（mailbox.wake_in）、停止（closing_on_stop）、等用户的调用（open_asks_of）、
  交回结局（work.deliver_in）都 match 它的结果（docs/architecture.md 2.4 第 2 行）。
- 什么时候继续（Paused.ready）：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；事件留在信箱里
  等它们一起交给模型。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Annotated, Final, get_args

from pydantic import Field, TypeAdapter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturn
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.answers import AgentAnswered
from muliao.sessions.approvals import ApprovalAnswered, requested_among, requested_in
from muliao.sessions.conventions import is_stopped, unanswered_calls
from muliao.sessions.holds import called_among, called_in
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


async def handed_out_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, HandedOut]:
    """handed_out_in 的批量版本（查询次数与会话数无关）。"""
    called = await called_among(s, session_ids)
    requested = await requested_among(s, session_ids)
    return {x: HandedOut(called[x], requested[x]) for x in session_ids}


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


# ---- 会话停在哪（docs/architecture.md 2.4 第 2 行）：唯一的判断是 position，各处 match 它的结果 ----


@dataclass(frozen=True)
class PendingCall:
    """一个挂起的调用与它是哪一种（kind_of 认出的）。"""

    call: ToolCallPart
    kind: DeferredKind


@dataclass(frozen=True)
class Idle:
    """空闲：没有挂起的调用、没有要续跑的，信箱也空了。"""


@dataclass(frozen=True)
class Fresh:
    """新开：没有挂起的调用、没有要续跑的，信箱里有信——作为下一次运行的开头交给模型。"""


@dataclass(frozen=True)
class Resend:
    """从历史续跑（pydantic-ai 的续跑语义）：历史停在一条没得到回复、没标停止的请求上（重发它，replays 为空），或最后
    的回复里没出结果的调用都能原样重新执行（replays：还没交出去的 ask_agent，按调用 id 幂等，DeferredKind.replayable）。"""

    replays: list[ToolCallPart]


@dataclass(frozen=True)
class Paused:
    """等回答：停在挂起的调用上（pending，按出现顺序）。answers 是信箱里对它们的回答（answer_mail 是带着这些回答的
    信件），ready 是现在能不能继续：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；否则等着，事件留在
    信箱里等它们一起交给模型。"""

    pending: dict[str, PendingCall]
    answers: dict[str, CallResult]
    answer_mail: list[Mail]
    ready: bool


@dataclass(frozen=True)
class Interrupted:
    """被打断：最后的回复里有没出结果、也不在挂起的调用（这一步在执行工具时被打断：进程退出，或这一步失败）。它们
    不重新执行（副作用可能已经发生），先补上「被中断」的结果（sessions.history.interrupted_results）再看 after：补完之后
    历史停在补出来的请求上（Resend），或停在同一条回复里别的挂起调用上（Paused，补过结果的调用在交回回答时被标为跳过）。

    只要有一个不能重放，整步都补、都不重放——pydantic-ai 续跑时要么执行这条回复里所有没结果的调用、要么一个都不
    执行；历史停在这条回复之后的请求上时，pydantic-ai 重发那条请求、不再执行这些调用，所以都补。"""

    calls: list[ToolCallPart]
    after: Resend | Paused


Position = Idle | Fresh | Resend | Paused | Interrupted


def position(history: Sequence[ModelMessage], handed: HandedOut, queued: Sequence[Mail]) -> Position:
    """会话现在停在哪（纯函数）。history 至少要含最后一条模型回复起的那一段（history.since_last_response：没出结果的
    调用只可能在那里）；handed 是交出去的永久事实；queued 是信箱里排着的信件。

    挂起的调用：没有结果的挂起工具调用（参数被拒绝、交还模型重试的有结果，不算）；要占用的（ask_agent，
    DeferredKind.needs_hold）只算已经交出去的——没有占用的 ask_agent 是工具还没执行完（回复保存后、投递提交前失败），
    是待执行的调用。其他工具的调用记过等批准的在等批准，没记过的是执行到一半被打断的。"""
    unanswered = unanswered_calls(history)
    pending: dict[str, PendingCall] = {}
    others: list[ToolCallPart] = []
    for c in unanswered:
        kind = kind_of(c, handed.approvals)
        if kind is not None and (not kind.needs_hold or c.tool_call_id in handed.delegated):
            pending[c.tool_call_id] = PendingCall(c, kind)
        else:
            others.append(c)
    last = history[-1] if history else None
    replays = isinstance(last, ModelResponse) and all(_replayable(c, handed) for c in others)
    after: Resend | Paused | None = None
    if pending:
        answer_mail = [m for m in queued if is_on_time(m, pending)]
        answers = {k: call_result(v) for m in answer_mail for k, v in m.answers.items()}
        ready = any(m.kind == MailKind.MESSAGE for m in queued) or set(pending) <= set(answers)
        after = Paused(pending, answers, answer_mail, ready)
    elif others:
        after = Resend(others if replays else [])
    elif isinstance(last, ModelRequest) and not is_stopped(last):
        after = Resend([])
    if others and not replays:
        if after is None:
            raise RuntimeError(f"interrupted calls without a way to go on: {others}")
        return Interrupted(others, after)
    if after is not None:
        return after
    return Fresh() if queued else Idle()


def _replayable(call: ToolCallPart, handed: HandedOut) -> bool:
    kind = kind_of(call, handed.approvals)
    return kind is not None and kind.replayable


def paused(at: Position) -> Paused | None:
    """挂着的调用（被打断时是补完结果之后仍挂着的），没有为 None。"""
    match at:
        case Paused():
            return at
        case Interrupted(after=Paused() as after):
            return after
        case _:
            return None


def is_on_time(mail: Mail, pending: Mapping[str, object]) -> bool:
    """这封回答信件针对的调用都还挂着：作为那些调用的结果交回。否则它是迟到的，作为文字交给模型（late_text）。"""
    return mail.kind == MailKind.ANSWER and set(mail.answers) <= set(pending)


@dataclass(frozen=True)
class StopClosing:
    """用户停止时怎么关闭没出结果的调用（sessions.halt.close_history_in）：

    - stopped：补「已停止」的结果——被打断的、能重放的，以及停止后不再挂着的挂起调用（DeferredKind.kept_open_on_stop
      为假）；
    - answered：停止后本该仍挂着、但回答已提交还排着的调用 -> 交给模型的结果。回答属于它回答的调用，是停止前用户
      已经给出的部分，照常写进历史（同「停止前已经产生的部分照常保存」），不留在信箱里等以后唤醒；
    - used：answered 的回答所在的信件，随之记为已处理（整封信：同一封里对等批准调用的决定也算已处理，那个调用
      已补「已停止」，只影响界面不划掉它，不拆信）。其余仍挂着、没有回答的调用照旧挂着，用户仍可回答。"""

    stopped: list[ToolCallPart]
    answered: list[tuple[ToolCallPart, object]]
    used: list[Mail]


def closing_on_stop(at: Position) -> StopClosing:
    match at:
        case Interrupted(calls=calls, after=after):
            rest = closing_on_stop(after)
            return StopClosing([*calls, *rest.stopped], rest.answered, rest.used)
        case Resend(replays=replays):
            return StopClosing(list(replays), [], [])
        case Paused(pending=pending, answers=answers, answer_mail=answer_mail):
            kept = {k: p for k, p in pending.items() if p.kind.kept_open_on_stop}
            # 回答怎么变成调用的结果只在 deferred_results 一处定
            results = deferred_results({k: p for k, p in kept.items() if k in answers}, answers)
            for call_id, value in results.items():
                if isinstance(value, ToolReturn):
                    # 停止时结果直接写进历史；ToolReturn 要由 pydantic-ai 转换成消息，停止后仍挂着的种类不能交回它
                    raise RuntimeError(
                        f"call {call_id} of {kept[call_id].call.tool_name}: a ToolReturn cannot be saved on stop"
                    )
            return StopClosing(
                stopped=[p.call for k, p in pending.items() if k not in kept],
                answered=[(kept[k].call, v) for k, v in results.items()],
                used=[m for m in answer_mail if set(m.answers) & set(results)],
            )
        case Idle() | Fresh():
            return StopClosing([], [], [])


def deferred_results(
    pending: Mapping[str, PendingCall],
    answers: Mapping[str, CallResult],
    *,
    replaced: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """交给 pydantic-ai 的挂起调用结果。有回答的用回答信封的 tool_return；其余（来了新消息，或这次只回答了一部分）用
    那一种信封的 skipped，由它看这次交回的别的回答自己定。

    replaced：另有结果、不用回答的已回答调用（如 sessions.approvals.take_in 给的「被中断」：批准之前已经交给过
    运行去执行，不再执行）。替换哪些、换成什么由那一种自己的模块定，这里只代入。"""
    replaced = replaced or {}
    results: dict[str, object] = {}
    for call_id, p in pending.items():
        result = answers.get(call_id)
        if result is None:
            results[call_id] = p.kind.answer.skipped(list(answers.values()))
        elif not isinstance(result, p.kind.answer):
            raise RuntimeError(f"call {call_id} of {p.call.tool_name} got an answer for {result.tool}")
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


def open_asks_of(at: Position, queued: Sequence[Mail]) -> OpenAsks:
    """会话停在哪（position）里挂着的、等用户的调用；回答已在信箱里排着的算已提交。提问是带问题的种类，批准是
    AWAITING_APPROVAL；ask_agent 不等用户，不在其中。"""
    submitted = frozenset(k for m in queued if m.kind == MailKind.ANSWER for k in m.answers)
    pending = {} if (p := paused(at)) is None else p.pending
    questions: dict[str, list[Question]] = {}
    gated: dict[str, str] = {}
    for k, c in pending.items():
        if c.kind.questions is not None:
            questions[k] = c.kind.questions(c.call)
        elif c.kind is AWAITING_APPROVAL:
            gated[k] = c.call.tool_name
    return OpenAsks(
        answerable={k: v for k, v in questions.items() if k not in submitted},
        approvable={k: v for k, v in gated.items() if k not in submitted},
        submitted=submitted,
        approvals_submitted=frozenset(k for k in gated if k in submitted),
    )
