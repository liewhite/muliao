"""一次运行怎么结束（AgentRuntime 交给调用方的结局），以及工具怎么让运行停在自己身上（挂起）。

运行时的端口不把模型库的运行与挂起类型交出去（docs/architecture.md 2.1）：别的领域只看到这里的 Finished / Paused、
交回挂起结果时给的 `{调用 id: 结果}`，工具挂起用 defer_call。换掉模型库时改动只在 agent_runtime 里。消息
（ModelMessage、ToolCallPart 等）是会话历史的存储形态，不在此列。
"""

from dataclasses import dataclass
from typing import NoReturn

from pydantic_ai import AgentRunResult, DeferredToolRequests
from pydantic_ai.exceptions import CallDeferred
from pydantic_ai.messages import ModelMessage, ToolCallPart


@dataclass(frozen=True)
class Finished:
    """运行给出了最终回答。messages 是这次运行新增的消息（不含传入的历史）。"""

    answer: str
    messages: list[ModelMessage]


@dataclass(frozen=True)
class Paused:
    """运行停在挂起的工具调用上（工具调用了 defer_call）：结果之后作为 deferred 交回，运行从那里继续。"""

    calls: tuple[ToolCallPart, ...]


def defer_call() -> NoReturn:
    """在工具里调用：这次调用挂起，运行停在这里正常结束（Paused），结果之后交回。"""
    raise CallDeferred()


def outcome_of(result: AgentRunResult[str | DeferredToolRequests]) -> Finished | Paused:
    output = result.output
    if isinstance(output, str):
        return Finished(answer=output, messages=result.new_messages())
    if output.approvals:
        # 本项目没有需要批准的工具（只用 defer_call 挂起）：出现说明工具与这里的约定不一致
        raise RuntimeError(f"run requested approvals: {[c.tool_name for c in output.approvals]}")
    return Paused(calls=tuple(output.calls))
