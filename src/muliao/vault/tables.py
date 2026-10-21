"""vault 的表。"""

from datetime import datetime

from sqlalchemy import Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref


class Secret(Base):
    """owner 为 None 时是公共 secret，否则为该 agent 私有；同名时私有遮蔽公共（见 ownership.shadowing）。"""

    __tablename__ = "secrets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner: Mapped[str | None] = mapped_column(agent_ref())
    key: Mapped[str] = mapped_column(String(100))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("uq_secrets_private", "owner", "key", unique=True, sqlite_where=owner.isnot(None)),
        Index("uq_secrets_public", "key", unique=True, sqlite_where=owner.is_(None)),
    )
