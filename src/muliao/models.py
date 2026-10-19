"""数据库表。每个领域事实只在这里存一份；文件型事实（skill、memory）见 docs/architecture.md。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.db import Base, UTCDateTime, utcnow


class Origin(StrEnum):
    USER = "user"
    CRON = "cron"
    WEBHOOK = "webhook"
    TELEGRAM = "telegram"
    DELEGATION = "delegation"


class DeliveryChannel(StrEnum):
    NONE = "none"
    WEBPUSH = "webpush"
    TELEGRAM = "telegram"


class TaskKind(StrEnum):
    TURN = "turn"
    REFLECTION = "reflection"
    DELIVER = "deliver"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


def _fk_agent() -> ForeignKey:
    return ForeignKey("agents.name", ondelete="CASCADE")


class Agent(Base):
    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text)
    reflect_every: Mapped[int]
    turns_since_reflection: Mapped[int] = mapped_column(default=0)
    # 已纳入反思的最大 message id；下一次反思从它之后开始
    reflection_checkpoint: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_name: Mapped[str] = mapped_column(_fk_agent(), index=True)
    title: Mapped[str] = mapped_column(String(200))
    origin: Mapped[Origin] = mapped_column(String(16))
    # 触发源内的引用：cron job id、webhook id、Telegram chat id、父 session id
    origin_ref: Mapped[str | None] = mapped_column(String(64))
    delivery: Mapped[DeliveryChannel] = mapped_column(String(16))
    # channel 内的投递目标，如 Telegram chat id；Web Push 为 None
    delivery_target: Mapped[str | None] = mapped_column(String(64))
    unread: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    __table_args__ = (Index("ix_sessions_origin_ref", "origin", "origin_ref"),)


class Message(Base):
    """一条 pydantic-ai ModelMessage，`payload` 为其 JSON。id 单调递增，用作反思区间。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int]
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # SQLite 默认按 max(rowid)+1 分配 id，删除尾部行后会复用；AUTOINCREMENT 保证单调
    __table_args__ = {"sqlite_autoincrement": True}


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[TaskKind] = mapped_column(String(16))
    # 同一 lane 内 FIFO 串行；见 docs/architecture.md「Lane」
    lane: Mapped[str] = mapped_column(String(100))
    # 系统通知（如推送测试）不属于任何 agent
    agent_name: Mapped[str | None] = mapped_column(_fk_agent(), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[TaskStatus] = mapped_column(String(16), default=TaskStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_tasks_status_lane", "status", "lane"), {"sqlite_autoincrement": True})


class Reflection(Base):
    __tablename__ = "reflections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(_fk_agent(), index=True)
    from_message_id: Mapped[int]
    to_message_id: Mapped[int]
    output: Mapped[str] = mapped_column(Text)
    messages: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CronJob(Base):
    __tablename__ = "cron_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    agent_name: Mapped[str] = mapped_column(_fk_agent())
    schedule: Mapped[str] = mapped_column(String(100))
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_session_id: Mapped[str | None] = mapped_column(String(36))
    # 触发失败时记录原因并停用该 job，修复后由用户重新启用
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    agent_name: Mapped[str] = mapped_column(_fk_agent())
    token: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # None 表示公共，所有 agent 可见
    owner: Mapped[str | None] = mapped_column(_fk_agent())
    name: Mapped[str] = mapped_column(String(64))
    transport: Mapped[McpTransport] = mapped_column(String(16))
    command: Mapped[str | None] = mapped_column(Text)
    args: Mapped[list[str]] = mapped_column(JSON, default=list)
    url: Mapped[str | None] = mapped_column(Text)
    # 值可包含 ${vault:KEY}，运行时解析
    env: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        Index("uq_mcp_private", "owner", "name", unique=True, sqlite_where=owner.isnot(None)),
        Index("uq_mcp_public", "name", unique=True, sqlite_where=owner.is_(None)),
    )


class Secret(Base):
    __tablename__ = "secrets"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
