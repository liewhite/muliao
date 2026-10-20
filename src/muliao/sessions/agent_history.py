"""一个 agent 在它全部会话里做过的事（会话领域给反思用的查询，docs/architecture.md 第 13 节「强制反思」）。事实只在 messages 表里：步数（模型
回复数）、消息 id 区间与那段记录都由它推导，不另存。消息 id 单调递增，区间 (from_id, to_id] 一经确定，里面的消息只会
因为会话被删除而减少。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.platform.db import Database
from muliao.sessions.codec import KIND_PATH, RESPONSE_KIND, load_messages
from muliao.sessions.tables import Message, Session
from muliao.sessions.transcript import to_items, to_text


class AgentHistory:
    def __init__(self, db: Database):
        self._db = db

    async def steps_after(self, agent_id: str, after_message_id: int) -> int:
        """这个 agent 在消息 after_message_id 之后的模型回复条数（每条回复就是一步）。"""
        async with self._db.transaction() as s:
            return await _steps_after_in(s, agent_id, after_message_id)

    async def window_after(self, agent_id: str, after_message_id: int) -> tuple[int, int | None]:
        """同一个快照里的步数与这个 agent 最后一条消息的 id（没有消息为 None）：有步数时后者一定大于
        after_message_id。"""
        async with self._db.transaction() as s:
            steps = await _steps_after_in(s, agent_id, after_message_id)
            last = await s.scalar(
                select(func.max(Message.id))
                .join(Session, Session.id == Message.session_id)
                .where(Session.agent_id == agent_id)
            )
            return steps, last

    async def transcript(self, agent_id: str, from_id: int, to_id: int, *, max_item_chars: int) -> str:
        """消息 (from_id, to_id] 的文字记录，按会话分段（按会话在区间里首次出现的先后）；区间里没有消息时为空串。"""
        async with self._db.transaction() as s:
            rows = list(
                (
                    await s.scalars(
                        select(Message)
                        .join(Session, Session.id == Message.session_id)
                        .where(Session.agent_id == agent_id, Message.id > from_id, Message.id <= to_id)
                        .order_by(Message.id)
                    )
                ).all()
            )
            sessions = {
                x.id: x
                for x in (
                    await s.scalars(select(Session).where(Session.id.in_({r.session_id for r in rows})))
                ).all()
            }
        by_session: dict[str, list[Message]] = {}
        for row in rows:
            by_session.setdefault(row.session_id, []).append(row)
        blocks = []
        for session_id, group in by_session.items():
            session = sessions[session_id]
            items = to_items(load_messages(r.payload for r in group))
            header = f"### Session: {session.title} (origin: {session.origin})"
            blocks.append(f"{header}\n{to_text(items, max_item_chars=max_item_chars)}")
        return "\n\n".join(blocks)


async def _steps_after_in(s: AsyncSession, agent_id: str, after_message_id: int) -> int:
    result = await s.execute(
        select(func.count(Message.id))
        .join(Session, Session.id == Message.session_id)
        .where(
            Session.agent_id == agent_id,
            Message.id > after_message_id,
            func.json_extract(Message.payload, KIND_PATH) == RESPONSE_KIND,
        )
    )
    return result.scalar_one()
