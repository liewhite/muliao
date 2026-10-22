"""会话上的任务（运行、压缩）在自己的事务里读它的会话与 agent，一处实现。"""

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.api import AgentInfo, agent_in
from muliao.sessions.tables import Session


async def load_session(s: AsyncSession, session_id: str) -> tuple[Session, AgentInfo]:
    """会话上的任务（运行、压缩）读它的会话与 agent。会话忙时不能删除，任务执行期间它一定还在。"""
    session = await s.get(Session, session_id)
    if session is None:
        raise RuntimeError(f"session {session_id} vanished while a task was working on it")
    return session, await agent_in(s, session.agent_id)
