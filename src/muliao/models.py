"""数据库表。每个领域事实只在这里存一份；文件型事实（skill、memory）见 docs/architecture.md。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.db import Base, StrEnumType, UTCDateTime, utcnow


class Origin(StrEnum):
    """会话来源：用户在网页发起、cron 定时触发、agent 之间委派。"""

    USER = "user"
    CRON = "cron"
    DELEGATION = "delegation"


class ReplyTo(StrEnum):
    """一轮结束后结果交给谁，按轮决定（见 docs/architecture.md「会话领域」）。

    只有 USER 会推送通知。AGENT 表示这一轮由另一个 agent 同步发问，回答经 waits 表交回给它；
    已有的 USER 轮次也可能被 wait_for 等待，等待关系同样只记在 waits 表。
    """

    USER = "user"
    AGENT = "agent"
    NONE = "none"


class TaskKind(StrEnum):
    TURN = "turn"
    REFLECTION = "reflection"
    DELIVER = "deliver"
    COMPACTION = "compaction"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    # 一轮对话挂起，等 thread 的结果；和 running 一样挡住同一 lane 的后续任务，结果齐了转回 pending 接着执行
    WAITING = "waiting"


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


def _fk_agent() -> ForeignKey:
    return ForeignKey("agents.id", ondelete="CASCADE")


class Agent(Base):
    __tablename__ = "agents"

    # 系统生成、不可变：主键、外键、目录名、URL 与工具调用都用它
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 仅供展示，可改名；唯一以免委派时混淆
    name: Mapped[str] = mapped_column(String(64), unique=True)
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
    agent_id: Mapped[str] = mapped_column(_fk_agent(), index=True)
    title: Mapped[str] = mapped_column(String(200))
    origin: Mapped[Origin] = mapped_column(StrEnumType(Origin))
    # cron 触发的会话记录 cron job id
    origin_ref: Mapped[str | None] = mapped_column(String(64))
    # 委派产生的子会话：父会话与它在父会话内的 thread 名（模型用 thread 名指代子会话）
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL", name="fk_sessions_parent_session_id"), index=True
    )
    thread: Mapped[str | None] = mapped_column(String(100))
    # 私有子会话归父会话所有：不进收件箱，用户只读，随父会话删除
    private: Mapped[bool] = mapped_column(default=False)
    unread: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    __table_args__ = (
        Index("ix_sessions_origin_ref", "origin", "origin_ref"),
        Index(
            "uq_sessions_thread",
            "parent_session_id",
            "thread",
            unique=True,
            sqlite_where=parent_session_id.isnot(None),
        ),
    )


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
    kind: Mapped[TaskKind] = mapped_column(StrEnumType(TaskKind))
    # 同一 lane 内 FIFO 串行；见 docs/architecture.md「Lane」
    lane: Mapped[str] = mapped_column(String(100))
    # 系统通知（如推送测试）不属于任何 agent
    agent_id: Mapped[str | None] = mapped_column(_fk_agent(), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[TaskStatus] = mapped_column(StrEnumType(TaskStatus), default=TaskStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    not_before: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (Index("ix_tasks_status_lane", "status", "lane"), {"sqlite_autoincrement": True})


class Wait(Base):
    """父 agent 挂起的一个工具调用在等哪个 turn 的结果。

    target_task_id 不设外键：目标所在 session 被删除时，要先给等待方写入错误结果，不能让级联删除悄悄抹掉等待关系。
    """

    __tablename__ = "waits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    waiter_task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    tool_call_id: Mapped[str] = mapped_column(String(100))
    target_task_id: Mapped[int] = mapped_column(index=True)
    # 二者恰有其一表示已结束：result 为目标的最终回答，error 为失败说明
    result: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Compaction(Base):
    """session 上下文压缩：id <= through_message_id 的消息由 summary 代替发给模型。原始消息仍保留。"""

    __tablename__ = "compactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    through_message_id: Mapped[int]
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Reflection(Base):
    __tablename__ = "reflections"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(_fk_agent(), index=True)
    from_message_id: Mapped[int]
    to_message_id: Mapped[int]
    output: Mapped[str] = mapped_column(Text)
    messages: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CronJob(Base):
    __tablename__ = "cron_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    agent_id: Mapped[str] = mapped_column(_fk_agent())
    schedule: Mapped[str] = mapped_column(String(100))
    prompt: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_session_id: Mapped[str | None] = mapped_column(String(36))
    # 触发失败时记录原因并停用该 job，修复后由用户重新启用
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class McpServer(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # None 表示公共，所有 agent 可见
    owner: Mapped[str | None] = mapped_column(_fk_agent())
    name: Mapped[str] = mapped_column(String(64))
    transport: Mapped[McpTransport] = mapped_column(StrEnumType(McpTransport))
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
