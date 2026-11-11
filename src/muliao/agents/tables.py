"""agent 领域的表。"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Index,
    String,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, UTCDateTime, utcnow


class Agent(Base):
    __tablename__ = "agents"
    # 系统默认 agent 最多一个（Web 新对话默认选中它）
    __table_args__ = (Index("uq_agents_default", "is_default", unique=True, sqlite_where=text("is_default")),)

    # 系统生成、不可变：主键、外键、目录名、URL 与工具调用都用它
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 名字只认这里（也是资源目录里它的目录名），别的领域在自己的事务里按名字解析、展示。仅供展示，可改名；唯一以免委派时混淆
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # 正在删除时是发起删除的人（actor）（docs/architecture.md 第 10 节）：不出现在列表与名称解析里，各领域清理完它的数据后删除
    # 这一行（它的资源目录同时删掉）
    deleting_by: Mapped[str | None] = mapped_column(String(64))
    # 系统默认 agent（AgentService.set_default）；行删除时设置随之消失，正在删除的不出现在列表里
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # 归档时间（AgentLifecycle.set_archived）：非空时不能运行（agents.active.runnable_row_in），取消归档后恢复
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
