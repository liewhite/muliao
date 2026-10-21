"""平台的表：任务队列（tasks，种类由各领域定义），以及别的领域引用 agent 与会话时用的不透明 id 列类型。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow

# 任务种类是不透明的字符串：各领域定义自己的种类（如会话的运行、通知的推送），在组合根为它登记处理器（Worker 的
# handlers），平台不枚举它们。平台自己只有一种：领域事件或定向命令的一次投递（platform.messaging）
EVENT_TASK = "event"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def agent_ref() -> String:
    """别的领域对 agent 的引用：不透明的 id，没有跨领域外键（删除 agent 时各领域按 AgentDeleting 清理自己的数据）。"""
    return String(32)


def session_ref() -> String:
    """别的领域对会话的引用：不透明的 id，没有跨领域外键（按 SessionDeleted 清理）。"""
    return String(36)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    # 同一 lane 内 FIFO 串行；见 docs/architecture.md「任务队列即 actor 调度」
    lane: Mapped[str] = mapped_column(String(100))
    # 系统通知（如推送测试）不属于任何 agent
    agent_id: Mapped[str | None] = mapped_column(agent_ref(), index=True)
    session_id: Mapped[str | None] = mapped_column(session_ref(), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[TaskStatus] = mapped_column(StrEnumType(TaskStatus), default=TaskStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_tasks_status_lane", "status", "lane"), {"sqlite_autoincrement": True})
