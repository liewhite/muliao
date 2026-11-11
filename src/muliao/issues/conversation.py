"""issue 会话的身份：每个 (issue, agent) 唯一的一个会话（#43），不存会话 id，由会话领域按发送方（IssueOrigin）与 agent
定位（sessions.api.SessionKey）。issue 只交出自己的 id，来源编号的格式归会话领域（归属表第 8 行）。"""

from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.api import (
    IssueOrigin,
    SenderMail,
    SessionActivity,
    SessionInfo,
    SessionKey,
    ToSender,
    WorkState,
    keyed_session_in,
    keyed_sessions_among,
)


def mail(issue_id: int, agent_id: str, title: str, content: str) -> SenderMail:
    """投进 agent 在这个 issue 上的会话的信，没有会话时以 title 新建。"""
    return SenderMail(
        sender=IssueOrigin(issue_id=issue_id),
        content=content,
        session=SessionKey(agent_id=agent_id, title=title),
    )


def recipient(issue_id: int) -> ToSender:
    """投进这个 issue 的事件之后问忙不忙的句柄（触发器防重叠用）：投给哪个 agent 由 issue 的规则在处理事件时才定，所以是
    这个 issue 上全部的 agent 会话：有一个不空闲就算忙。"""
    return ToSender(sender=IssueOrigin(issue_id=issue_id))


async def session_in(s: AsyncSession, issue_id: int, agent_id: str) -> SessionInfo | None:
    """agent 在这个 issue 上的会话；还没有（或已删除）为 None。"""
    return await keyed_session_in(s, IssueOrigin(issue_id=issue_id), agent_id)


async def sessions_among(s: AsyncSession, issue_ids: Collection[int]) -> dict[int, dict[str, SessionInfo]]:
    """每个 issue -> {agent id -> 它在这个 issue 上的会话}。"""
    found = await keyed_sessions_among(s, [IssueOrigin(issue_id=i) for i in issue_ids])
    return {i: found[IssueOrigin(issue_id=i)] for i in issue_ids}


async def work_states_in(
    activity: SessionActivity, s: AsyncSession, keys: Collection[tuple[int, str]]
) -> dict[tuple[int, str], WorkState | None]:
    """每个 (issue, agent) 的会话在跑 / 在等 / 空闲（SessionActivity.keyed_work_states_in）。"""
    states = await activity.keyed_work_states_in(s, [(IssueOrigin(issue_id=i), a) for i, a in keys])
    return {(i, a): states[(IssueOrigin(issue_id=i), a)] for i, a in keys}
