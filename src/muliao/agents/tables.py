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
    # 仅供展示，可改名；唯一以免委派时混淆
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    # 分发规则（agents.permissions）：角色名（不含每个 agent 隐含的 base）与它自己的 allow / deny（`kind:glob`）
    roles: Mapped[list[str]] = mapped_column(JSON)
    allow: Mapped[list[str]] = mapped_column(JSON, server_default=text("'[]'"))
    deny: Mapped[list[str]] = mapped_column(JSON, server_default=text("'[]'"))
    # 思考档位（agents.service.Thinking）；"default" 表示不传、按模型与服务端默认
    thinking: Mapped[str] = mapped_column(String(16), default="default", server_default="default")
    # 正在删除时是发起删除的作者（docs/architecture.md 第 10 节）：不出现在列表与名称解析里，各领域清理完它的数据后删除
    # 这一行（它名下的条目与记忆随之删除，删除的版本记这个作者）
    deleting_by: Mapped[str | None] = mapped_column(String(64))
    # 系统默认 agent（AgentService.set_default）；行删除时设置随之消失，正在删除的不出现在列表里
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # 分组（只用于展示与列表的组织，如一批同类的接单 agent）；None 为不分组
    group: Mapped[str | None] = mapped_column(String(64))
    # 归档时间（AgentLifecycle.set_archived）：非空时不能运行（agents.active.runnable_row_in），取消归档后恢复
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Role(Base):
    """角色可编辑的规则（agents.roles）：用户自建的角色，与内置角色 base、admin 可编辑的部分（它们固定的部分在
    agents.permissions，行可以不存在）。自建的被 agent 引用时不能删，内置的不能删。"""

    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    allow: Mapped[list[str]] = mapped_column(JSON)
    deny: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


def _owner_ref() -> ForeignKey:
    # 同一领域内的外键：agent 的行删除时它名下的条目与记忆一起删除（AgentLifecycle.on_sessions_cleared）
    return ForeignKey("agents.id", ondelete="CASCADE")


class Item(Base):
    """条目（agents.items）：skill、MCP server、secret，按种类名字全局唯一。owner 为 None 是平台的，否则是该 agent 的；
    谁能用、谁能改见 agents.access。

    content 的含义因种类而异：skill 是 SKILL.md 全文（附件在 item_files），MCP server 是配置 JSON（McpConfig），secret 为空
    （密文在 secret，由 vault 加解密）。description 是 skill 与 secret 的描述（skill 的也在 SKILL.md 的 frontmatter 里，
    写入时解析出来存一份，列 skill 索引不必解析全文）。"""

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
    """skill 的附件（SKILL.md 之外的文件：脚本、配置、参考文档），UTF-8 文本。"""

    __tablename__ = "item_files"

    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    # 相对 skill 目录的路径（posix）
    path: Mapped[str] = mapped_column(String(500), primary_key=True)
    content: Mapped[str] = mapped_column(Text)
    executable: Mapped[bool] = mapped_column(Boolean, default=False)


class Memory(Base):
    """记忆（agents.memory）：全局记忆（owner 为 None，至多一行）与每个 agent 的记忆（创建 agent 时写入）。"""

    __tablename__ = "memories"
    __table_args__ = ({"sqlite_autoincrement": True},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 唯一（SQLite 里 NULL 互不相等：全局记忆只有一行由 MemoryStore 保证，写入都在串行的写事务里）
    owner: Mapped[str | None] = mapped_column(String(32), _owner_ref(), unique=True)
    content: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Revision(Base):
    """条目、记忆、角色与 agent 分发规则的一版（agents.revisions）：谁、何时、改前改后、说明。只追加；对象或 agent
    删除后仍保留（审计）。

    subject 是 kinds.Subject（skill、mcp、secret、memory、role、agent）；name 是条目名、角色名，记忆与 agent 规则是
    agent 的 id（全局记忆为空串）。before / after 是这一版前后的文档（各对象自己的文本形式，见 agents.revisions 的
    模块说明），新建时 before 为 None、删除时 after 为 None。secret 的文档只有描述，不含值。"""

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
    author: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
