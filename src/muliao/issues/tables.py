"""issue 领域的表：issue 本身，与它的时间线（评论与每一次属性变化）。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref, session_ref


class IssueStatus(StrEnum):
    BACKLOG = "backlog"
    # 该由被指派的 agent 做：动态投给它。它在等什么（后台任务、委派、到点唤醒）由它的会话自己的等待看出，在等上游
    # 由依赖派生，都不另设状态（#261）
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
    # 依赖（上游）列表被修改：change 为 {"from": [issue id...], "to": [issue id...]}
    DEPENDS_ON = "depends_on"
    # 一个上游完成（done / cancelled）：change 为 {"issue": 上游 id, "from": 原状态, "to": 新状态}
    DEPENDENCY = "dependency"
    # 某个 agent 在它这个 issue 的会话里的运行最终失败了：actor 是该 agent，body 是错误摘要（RunFailed.summary）
    RUN_FAILED = "run_failed"
    # 某个 agent 在它这个 issue 的会话里的一次运行结束了，issue 却还是 in_progress、没人在做（rules.stalled）：平台记下并投进
    # 那个会话，请它表态（做完、blocked，或用会话的等待手段挂起）。actor 是该 agent
    UNSETTLED = "unsettled"
    # actor 用 issue_stop 停掉了被指派 agent 在这个 issue 上的会话：change 为 {"from": null, "to": 那个 agent 的 id}
    STOPPED = "stopped"


class Issue(Base):
    """人与 agent 协作的一件事。

    owner_agent_id 是负责这件事的项目（agent），被指派人是现在谁在做。
    被指派人 assignee：agent id、rules.USER（用户，只有一个），或 None（未指派）。
    每个 agent 在这个 issue 上的会话归会话领域：按发送方（origin=issue, origin_ref=issue id）与 agent 定位，这里不存。
    """

    __tablename__ = "issues"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[IssueStatus] = mapped_column(StrEnumType(IssueStatus))
    priority: Mapped[IssuePriority] = mapped_column(StrEnumType(IssuePriority))
    # agent id 或 rules.USER：列宽同 agent 的引用（agent_ref），但不只存 agent
    assignee: Mapped[str | None] = mapped_column(agent_ref(), index=True)
    # 负责的项目（agent）：创建时必填，不随指派变化，只能显式修改；按它筛选看一个项目的全部来龙去脉。
    # 只在负责的 agent 被删除时变为空
    owner_agent_id: Mapped[str | None] = mapped_column(agent_ref(), index=True)
    # 创建者：agent id，用户创建的为 USER（agents.api）
    creator: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("ix_issues_status", "status"), {"sqlite_autoincrement": True})


class IssueActivity(Base):
    """issue 时间线上的一条：评论（body），或一次属性变化（change 为 {"from": ..., "to": ...}）。只增不改。

    actor 为做这件事的 agent 的 id，用户做的为 USER（agents.api），系统自己的清理（删除 agent）为 rules.SYSTEM_ACTOR。
    RUN_FAILED 由平台记下，actor 是运行失败的那个 agent（这件事的主体）。
    session_id 是这件事发生在哪个会话：agent 用工具时所在的会话，UNSETTLED / RUN_FAILED 为那个 issue 会话；用户在界面上的
    操作、系统的清理没有会话（NULL）。会话被删除时置空（订阅 SessionDeleted）。
    """

    __tablename__ = "issue_activities"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(32))
    kind: Mapped[ActivityKind] = mapped_column(StrEnumType(ActivityKind))
    body: Mapped[str | None] = mapped_column(Text)
    change: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    session_id: Mapped[str | None] = mapped_column(session_ref(), index=True)

    __table_args__ = ({"sqlite_autoincrement": True},)


class IssueDependency(Base):
    """issue 之间的依赖（有向无环图）：issue_id 依赖 depends_on_id（上游）。任一端被删除，这条边随之删除。"""

    __tablename__ = "issue_dependencies"

    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True)
    depends_on_id: Mapped[int] = mapped_column(
        ForeignKey("issues.id", ondelete="CASCADE"), primary_key=True, index=True
    )
