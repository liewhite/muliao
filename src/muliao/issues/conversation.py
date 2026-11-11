"""issue 会话的身份：每个 (issue, agent) 唯一的一个会话（#43），不存会话 id，由会话领域按发送方（Origin.ISSUE，origin_ref
为 issue 号）与 agent 定位（sessions.api.SessionKey）。这个身份怎么拼、怎么解只在这里。"""

from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import CorruptState
from muliao.sessions.api import (
    Origin,
    SenderMail,
    SessionActivity,
    SessionInfo,
    SessionKey,
    WorkState,
    keyed_session_in,
    keyed_sessions_among,
)


def issue_of(session_id: str, origin_ref: str | None) -> int:
    """issue 会话（origin 是 Origin.ISSUE）的 issue 号：缺了是存下的数据坏了，不当成别的来源静默跳过。"""
    if origin_ref is None:
        raise CorruptState(f"issue session {session_id} has no issue number (origin_ref)")
    return int(origin_ref)


def mail(issue_id: int, agent_id: str, title: str, content: str) -> SenderMail:
    """投进 agent 在这个 issue 上的会话的信，没有会话时以 title 新建。"""
    return SenderMail(
        origin=Origin.ISSUE,
        origin_ref=str(issue_id),
        content=content,
        session=SessionKey(agent_id=agent_id, title=title),
    )


async def session_in(s: AsyncSession, issue_id: int, agent_id: str) -> SessionInfo | None:
    """agent 在这个 issue 上的会话；还没有（或已删除）为 None。"""
    return await keyed_session_in(s, Origin.ISSUE, str(issue_id), agent_id)


async def sessions_among(s: AsyncSession, issue_ids: Collection[int]) -> dict[int, dict[str, SessionInfo]]:
    """每个 issue -> {agent id -> 它在这个 issue 上的会话}。"""
    found = await keyed_sessions_among(s, Origin.ISSUE, [str(i) for i in issue_ids])
    return {i: found[str(i)] for i in issue_ids}


async def work_states_in(
    activity: SessionActivity, s: AsyncSession, keys: Collection[tuple[int, str]]
) -> dict[tuple[int, str], WorkState | None]:
    """每个 (issue, agent) 的会话在跑 / 在等 / 空闲（SessionActivity.keyed_work_states_in）。"""
    states = await activity.keyed_work_states_in(s, Origin.ISSUE, [(str(i), a) for i, a in keys])
    return {(i, a): states[(str(i), a)] for i, a in keys}
