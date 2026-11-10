"""agent 领域的表。"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, UTCDateTime, utcnow


class Agent(Base):
    __tablename__ = "agents"
    # 系统默认 agent 最多一个（Web 新对话默认选中它）
    __table_args__ = (Index("uq_agents_default", "is_default", unique=True, sqlite_where=text("is_default")),)

    # 系统生成、不可变：主键、外键、目录名、URL 与工具调用都用它
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # name / description / model / thinking / group 是资源目录（`<名>/agent.toml` 与目录名）的镜像，由 agents.source 同步：
    # 别的领域在自己的事务里按名字解析、展示；事实来源是文件。名字仅供展示，可改名；唯一以免委派时混淆
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    # 思考档位（agents.service.Thinking）；"default" 表示不传、按模型与服务端默认
    thinking: Mapped[str] = mapped_column(String(16), default="default", server_default="default")
    # 正在删除时是发起删除的人（actor）（docs/architecture.md 第 10 节）：不出现在列表与名称解析里，各领域清理完它的数据后删除
    # 这一行（它的资源目录同时删掉）
    deleting_by: Mapped[str | None] = mapped_column(String(64))
    # 系统默认 agent（AgentService.set_default）；行删除时设置随之消失，正在删除的不出现在列表里
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # 分组（只用于展示与列表的组织，如一批同类的接单 agent）；None 为不分组
    group: Mapped[str | None] = mapped_column(String(64))
    # 归档时间（AgentLifecycle.set_archived）：非空时不能运行（agents.active.runnable_row_in），取消归档后恢复
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ---- 以下是 #498 之前存在数据库里的 AI 资源（现在是资源目录里的文件，agents.assets）：没有代码再读写，
# 表先保留，回滚到旧版本时还能用；#503 另加迁移删除 ----


def _owner_ref() -> ForeignKey:
    # 同一领域内的外键：agent 的行删除时它名下的旧行一起删除
    return ForeignKey("agents.id", ondelete="CASCADE")


class AgentRulesRow(Base):
    """旧：agent 的角色名与它自己的 allow / deny（现在在 access.toml 的 [agents.<名>]）。"""

    __tablename__ = "agent_rules"

    agent_id: Mapped[str] = mapped_column(String(32), _owner_ref(), primary_key=True)
    roles: Mapped[list[str]] = mapped_column(JSON)
    allow: Mapped[list[str]] = mapped_column(JSON)
    deny: Mapped[list[str]] = mapped_column(JSON)


class RoleRow(Base):
    """旧：角色可编辑的规则（现在在 access.toml 的 [roles.<名>]）。"""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    allow: Mapped[list[str]] = mapped_column(JSON)
    deny: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Item(Base):
    """旧：skill、MCP server、secret、记忆（现在是资源目录里的文件）。owner 为 None 是平台的；content 是 SKILL.md 全文、MCP
    配置 JSON 或记忆正文，secret 的密文在 secret 列。"""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("kind", "name", name="uq_items_kind_name"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100))
    owner: Mapped[str | None] = mapped_column(String(32), _owner_ref(), index=True)
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    content: Mapped[str] = mapped_column(Text, default="", server_default="")
    secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class ItemFile(Base):
    """旧：skill 的附件（SKILL.md 之外的文件），UTF-8 文本。"""

    __tablename__ = "item_files"

    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    # 相对 skill 目录的路径（posix）
    path: Mapped[str] = mapped_column(String(500), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    executable: Mapped[bool] = mapped_column(Boolean, default=False)


class Revision(Base):
    """旧：条目、记忆、角色与 agent 分发规则的修改历史（谁、何时、改前改后、说明）；资源改成文件后没有版本。"""

    __tablename__ = "revisions"
    __table_args__ = (
        Index("ix_revisions_subject", "subject", "name", "id"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    subject: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100))
    before: Mapped[str | None] = mapped_column(Text)
    after: Mapped[str | None] = mapped_column(Text)
    # "user"、做出修改的 agent 的 id（可能已删除），或系统（迁移）
    actor: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
