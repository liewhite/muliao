"""等用户批准的工具调用（#530）：工具在执行前调用 agent_runtime 的 require_approval（如 skill_run 执行 SKILL.md 里
标了 approval 的脚本），运行停在这里正常结束；用户在界面上批准或拒绝，决定作为回答信件交回，批准后工具按原参数
执行，拒绝时说明作为这次调用的结果交给模型。它是挂起调用的一种，挂起、唤醒与交回的规则在 sessions.pending，设计见
docs/architecture.md「挂起的调用」。

等批准的调用不按工具名认（同一个工具多数调用不用批准）：运行停下时把这些调用记进 approval_requests，没有结果、
记过的调用就是在等批准。可能要批准的工具是普通工具，其余规则同普通工具：被打断不重放，用户停止时补「已停止」。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agent_runtime.api import Approval
from muliao.platform.db import utcnow
from muliao.sessions.tables import ApprovalRequest

APPROVAL: Literal["approval"] = "approval"

DENIED = "The user denied this call; it was not run."
# 没批准就继续了（来了新消息，或这次只回答了别的挂起调用）
NOT_DECIDED = (
    "The user did not approve this call and responded with something else instead; it was not run. "
    "Call it again if it is still needed."
)
# 批准了，但这次运行之前的尝试已经开始过（sessions.pending.deferred_results 的 retried）：可能已在执行中被打断，不再执行
INTERRUPTED = (
    "The user approved this call, but the run was interrupted (e.g. the service restarted) after it may have "
    "started executing, so it was not run again. It may have taken effect: check before calling it again "
    "(that asks the user to approve again)."
)


class Decision(BaseModel):
    """用户对一次等批准的调用的决定。"""

    approved: bool


class ApprovalAnswered(BaseModel):
    """回答信件里一次等批准的调用的结果：用户的决定。信封的 tool 是 approval，不是调用的工具名（同一个工具多数
    调用不用批准）。"""

    tool: Literal["approval"]
    result: Decision

    @classmethod
    def of(cls, approved: bool) -> ApprovalAnswered:
        return cls(tool=APPROVAL, result=Decision(approved=approved))

    def tool_return(self) -> object:
        return Approval(approved=True) if self.result.approved else Approval(approved=False, note=DENIED)


def skipped() -> Approval:
    return Approval(approved=False, note=NOT_DECIDED)


def late_lines(decisions: Sequence[ApprovalAnswered]) -> list[str]:
    """已经结束的等批准调用的决定（sessions.pending.late_text）：那次调用已经带着「没批准」结束了，没有执行。"""
    return [
        f"The user {'approved' if d.result.approved else 'denied'} a call that had already been closed "
        "without running; it was not run."
        for d in decisions
    ]


def waiting_text(tools: Sequence[str]) -> str:
    """运行停在等批准上时给用户的通知正文。"""
    return "等你批准：" + "、".join(tools)


async def record_in(s: AsyncSession, session_id: str, call_ids: Collection[str]) -> None:
    """这些调用停下来等批准了（运行收尾的事务里）。已记过的不再记（之前的尝试已收尾过）。"""
    known = await requested_in(s, session_id)
    for call_id in call_ids:
        if call_id not in known:
            s.add(ApprovalRequest(session_id=session_id, call_id=call_id, created_at=utcnow()))
    await s.flush()


async def requested_in(s: AsyncSession, session_id: str) -> set[str]:
    """这个会话里停下来等过批准的调用 id（不论现在有没有结果）。"""
    rows = await s.scalars(select(ApprovalRequest.call_id).where(ApprovalRequest.session_id == session_id))
    return set(rows.all())


async def requested_among(s: AsyncSession, session_ids: Collection[str]) -> dict[str, set[str]]:
    """requested_in 的批量版本（一次查询），每个会话都在结果里。"""
    found: dict[str, set[str]] = {x: set() for x in session_ids}
    rows = await s.execute(
        select(ApprovalRequest.session_id, ApprovalRequest.call_id).where(
            ApprovalRequest.session_id.in_(list(session_ids))
        )
    )
    for session_id, call_id in rows.all():
        found[session_id].add(call_id)
    return found
