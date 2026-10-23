"""agent 领域的表。"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, Index, String, Text, false, text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, UTCDateTime, utcnow


class Agent(Base):
    __tablename__ = "agents"
    # 系统默认 agent 最多一个（Web 新对话默认选中它）
    __table_args__ = (Index("uq_agents_default", "is_default", unique=True, sqlite_where=text("is_default")),)

    # 系统生成、不可变：主键、外键、目录名、URL 与工具调用都用它
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 仅供展示，可改名；唯一以免委派时混淆
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    # 工具权限（agents.permissions）：角色名（内置或 roles 表里的；public 自动拥有，不存）与它自己的 allow / deny 通配符
    roles: Mapped[list[str]] = mapped_column(JSON)
    allow: Mapped[list[str]] = mapped_column(JSON, server_default=text("'[]'"))
    deny: Mapped[list[str]] = mapped_column(JSON, server_default=text("'[]'"))
    # 思考档位（agents.service.Thinking）；"default" 表示不传、按模型与服务端默认
    thinking: Mapped[str] = mapped_column(String(16), default="default", server_default="default")
    # 正在删除时是发起删除的作者（docs/architecture.md 第 10 节）：不出现在列表与名称解析里，各领域清理完它的数据后删除
    # 这一行与资源目录（资源仓库的删除提交记这个作者）
    deleting_by: Mapped[str | None] = mapped_column(String(64))
    # 系统默认 agent（AgentService.set_default）；行删除时设置随之消失，正在删除的不出现在列表里
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Role(Base):
    """用户自建的角色（内置角色在 agents.permissions 里定义，不入库）。被 agent 引用时不能删。"""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    allow: Mapped[list[str]] = mapped_column(JSON)
    deny: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
