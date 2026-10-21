"""issue 领域的表：issue 本身，与它的时间线（评论与每一次属性变化）。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref


class IssueStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class IssuePriority(StrEnum):
    NONE = "none"
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ActivityKind(StrEnum):
    CREATED = "created"
    COMMENT = "comment"
    STATUS = "status"
    ASSIGNEE = "assignee"
    PRIORITY = "priority"
    OWNER = "owner"
    TITLE = "title"
    DESCRIPTION = "description"
    # agent 让出 issue：断开它的处理会话
    RELEASED = "released"


class Issue(Base):
    """人与 agent 协作的一件事。

    owner_agent_id 是负责这件事的项目（agent），被指派人是现在谁在做。
    被指派人：assignee_agent_id 为 agent；assigned_to_user 为真时指派给用户（只有一个用户）；两者都空为未指派。
    sessions 记每个 agent 在这个 issue 上的会话（agent id -> 会话 id）：同一个 agent 每次被唤醒都续写那个会话，
    上下文连贯；会话被删了就新开一个。
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[IssueStatus] = mapped_column(StrEnumType(IssueStatus))
    priority: Mapped[IssuePriority] = mapped_column(StrEnumType(IssuePriority))
    assignee_agent_id: Mapped[str | None] = mapped_column(agent_ref(), index=True)
    assigned_to_user: Mapped[bool] = mapped_column(default=False)
    # 负责的项目（agent）：创建时必填，不随指派变化，只能显式修改；按它筛选看一个项目的全部来龙去脉。
    # 只在负责的 agent 被删除时变为空
    owner_agent_id: Mapped[str | None] = mapped_column(agent_ref(), index=True)
    # 创建者：agent id，用户创建的为 None
    creator_agent_id: Mapped[str | None] = mapped_column(agent_ref())
    sessions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_issues_status", "status"), {"sqlite_autoincrement": True})


class IssueActivity(Base):
    """issue 时间线上的一条：评论（body），或一次属性变化（change 为 {"from": ..., "to": ...}）。只增不改。

    actor_agent_id 为做这件事的 agent，用户做的为 None。
    """

    __tablename__ = "issue_activities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor_agent_id: Mapped[str | None] = mapped_column(agent_ref())
    kind: Mapped[ActivityKind] = mapped_column(StrEnumType(ActivityKind))
    body: Mapped[str | None] = mapped_column(Text)
    change: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    __table_args__ = ({"sqlite_autoincrement": True},)
