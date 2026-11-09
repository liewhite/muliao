"""一次运行怎么结束（AgentRuntime 交给调用方的结局），以及工具怎么让运行停在自己身上（挂起）。

运行时的端口不把模型库的运行与挂起类型交出去（docs/architecture.md 2.1）：别的领域只看到这里的 Finished / Paused、
交回挂起结果时给的 `{调用 id: 结果}`（等批准的调用的结果是 Approval），工具挂起用 defer_call、等批准用
require_approval / is_approved。换掉模型库时改动只在 agent_runtime 里。消息（ModelMessage、ToolCallPart 等）是会话历史的存储形态，不在此列。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from pydantic_ai import AgentRunResult, DeferredToolRequests, DeferredToolResults, RunContext, ToolDenied
from pydantic_ai.exceptions import ApprovalRequired, CallDeferred
from pydantic_ai.messages import ModelMessage, ToolCallPart


@dataclass(frozen=True)
class Finished:
    """运行给出了最终回答。messages 是这次运行新增的消息（不含传入的历史）。"""

    answer: str
    messages: list[ModelMessage]


@dataclass(frozen=True)
class Paused:
    """运行停在挂起的工具调用上：calls 调用了 defer_call，结果之后作为 deferred 交回；approvals 调用了
    require_approval，之后交回 Approval。运行从那里继续。"""

    calls: tuple[ToolCallPart, ...]
    approvals: tuple[ToolCallPart, ...] = ()


@dataclass(frozen=True)
class Approval:
    """对一次等批准的调用的决定（作为它在 deferred 里的结果交回）：批准时工具按原参数再执行一次（工具里
    is_approved 为真），结果就是它的执行结果；不批准时 note 作为这次调用的结果交给模型，工具不执行。"""

    approved: bool
    note: str = ""


def defer_call() -> NoReturn:
    """在工具里调用：这次调用挂起，运行停在这里正常结束（Paused），结果之后交回。"""
    raise CallDeferred()


def require_approval() -> NoReturn:
    """在工具里调用（参数都校验过之后）：这次调用要先得到批准，运行停在这里正常结束（Paused.approvals）；批准后
    工具以同样的参数再被调用一次，那时 is_approved 为真。"""
    raise ApprovalRequired()


def is_approved(ctx: RunContext[Any]) -> bool:
    """这次调用是批准之后的再执行（见 require_approval）。"""
    return ctx.tool_call_approved


def outcome_of(result: AgentRunResult[str | DeferredToolRequests]) -> Finished | Paused:
    output = result.output
    if isinstance(output, str):
        return Finished(answer=output, messages=result.new_messages())
    return Paused(calls=tuple(output.calls), approvals=tuple(output.approvals))


def deferred_tool_results(deferred: Mapping[str, object]) -> DeferredToolResults:
    """交回的 `{调用 id: 结果}` 换成模型库的形态：Approval 是对等批准的调用的决定（批准→工具再执行，不批准→note
    作为结果），其余是调用的结果本身（等批准的调用也可以直接给结果，工具不执行）。"""
    approvals: dict[str, bool | ToolDenied] = {}
    calls: dict[str, object] = {}
    for call_id, result in deferred.items():
        if isinstance(result, Approval):
            approvals[call_id] = True if result.approved else ToolDenied(result.note)
        else:
            calls[call_id] = result
    return DeferredToolResults(calls=calls, approvals=approvals)
