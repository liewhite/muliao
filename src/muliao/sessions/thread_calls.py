"""调用开出的 thread（thread_calls 表的唯一读写处）：哪次工具调用开出或续写了哪个 thread。开出/续写 thread 的事务里
记下（sessions.threads），会话接口按调用 id 把 thread 挂在调用条目上（sessions.service），不看调用的结果。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.sessions.tables import ThreadCall


async def record_call_in(s: AsyncSession, parent_id: str, call_id: str, thread_id: str) -> None:
    """记下这次调用开出或续写了这个 thread。续跑重新执行同一次调用时已经记过，不再记；同一次调用指向别的 thread
    是 bug。"""
    existing = await s.scalar(
        select(ThreadCall.thread_session_id).where(
            ThreadCall.parent_session_id == parent_id, ThreadCall.call_id == call_id
        )
    )
    if existing is not None:
        if existing != thread_id:
            raise RuntimeError(
                f"call {call_id} of session {parent_id} already opened thread {existing}, not {thread_id}"
            )
        return
    s.add(ThreadCall(parent_session_id=parent_id, call_id=call_id, thread_session_id=thread_id))
    await s.flush()


async def call_threads_in(s: AsyncSession, parent_id: str) -> dict[str, str]:
    """这个会话里每次开出或续写 thread 的调用 → 那个 thread（已删除的 thread 随之删除，不在结果里）。"""
    rows = await s.execute(
        select(ThreadCall.call_id, ThreadCall.thread_session_id).where(
            ThreadCall.parent_session_id == parent_id
        )
    )
    return {call_id: thread_id for call_id, thread_id in rows.all()}
