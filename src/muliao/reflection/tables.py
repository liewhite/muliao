"""反思领域的表。"""

from datetime import datetime

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref


class Reflection(Base):
    __tablename__ = "reflections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(agent_ref(), index=True)
    from_message_id: Mapped[int]
    to_message_id: Mapped[int]
    output: Mapped[str] = mapped_column(Text)
    messages: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ReflectionCheckpoint(Base):
    """反思领域的检查点：该 agent 已纳入反思的最大 message id，下一次反思从它之后开始（reflection.service）。"""

    __tablename__ = "reflection_checkpoints"

    agent_id: Mapped[str] = mapped_column(agent_ref(), primary_key=True)
    message_id: Mapped[int] = mapped_column(default=0)
