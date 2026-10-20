"""数据库表。每个领域事实只在这里存一份；文件型事实（skill、memory）见 docs/architecture.md。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.db import Base, StrEnumType, UTCDateTime, utcnow


class Origin(StrEnum):
    """会话来源：用户在网页发起、触发器（定时、外部事件……）、agent 之间委派。"""

    USER = "user"
    TRIGGER = "trigger"
    DELEGATION = "delegation"


class ReplyTo(StrEnum):
    """一轮结束后结果交给谁，按轮决定（见 docs/architecture.md「会话领域」）。

    只有 USER 会推送通知。AGENT 表示这一轮由另一个 agent 用 ask_agent 发问，回答作为工具结果直接交回给它。
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
    # 选用的工具组名称（见 runtime.runner.ToolGroups）；自我改进的工具总是开放
    tool_groups: Mapped[list[str]] = mapped_column(JSON)
    # 指令不在这里：它在资源仓库的 agents/<id>/PROMPT.md（读写都经过 evolution.prompts.PromptStore）
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


class Trigger(Base):
    """触发器：具名、持久的事件来源，把外部刺激变成交给某个 agent 的一轮。

    怎么被触发由 kind 决定（定时、webhook……），只在代码里多态实现（triggers/），表里不为某种方式开专门的列：
    config 是这种方式的配置（用户或 agent 设置），state 是这种方式自己维护的运行状态（如下次触发时间、token 摘要）。
    """

    __tablename__ = "triggers"
    # id 是会话的发送方身份（sessions.origin_ref），必须永不复用
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 出现在 webhook 地址里：小写字母、数字与 -
    name: Mapped[str] = mapped_column(String(64), unique=True)
    agent_id: Mapped[str] = mapped_column(_fk_agent())
    # 交给 agent 的说明：这类事件是什么、该怎么处理；事件内容（若有）附在其后
    prompt: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_session_id: Mapped[str | None] = mapped_column(String(36))
    # 触发前的前置检查（triggers.precheck.Precheck），所有触发方式共用；None 表示每次都交给 agent
    precheck: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # 最近一次跑前置检查的时间（不论结果），让用户看到「在检查、只是没事」
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # fire_when = changed 时最近一次成功检查的输出，用来比较；修改前置检查时清空（重新记录基线）。
    # 最大可到事件上限，默认不随行加载（列表、定时循环都不需要它），用到时显式 undefer
    precheck_output: Mapped[str | None] = mapped_column(Text, deferred=True)
    # 最近一次触发或前置检查失败的原因（如定时触发器的数据不合法而被停用、检查脚本出错）；检查成功时清除
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Secret(Base):
    """owner 为 None 时是公共 secret，否则为该 agent 私有；同名时私有遮蔽公共（见 ownership.visible）。"""

    __tablename__ = "secrets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner: Mapped[str | None] = mapped_column(_fk_agent())
    key: Mapped[str] = mapped_column(String(100))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    description: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("uq_secrets_private", "owner", "key", unique=True, sqlite_where=owner.isnot(None)),
        Index("uq_secrets_public", "key", unique=True, sqlite_where=owner.is_(None)),
    )


class SessionEvent(Base):
    """交给某个会话的 agent、等下一轮一起处理的事件（后台任务完成、运行中的检查……）。设计见 runtime/events。

    一轮开始时取走会话里所有还没处理的事件，提交时记下 consumed_by；会话在等用户回答提问时事件留着，回答或跳过的
    那一轮一起带上。
    """

    __tablename__ = "session_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    # 谁发的，如 "job:12"（交给模型时标在事件上）
    source: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    # 处理它的那一轮的回答要不要推送给用户（运行中的检查不推送，完成通知随发起任务的那一轮）
    reply_to: Mapped[ReplyTo] = mapped_column(StrEnumType(ReplyTo))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 处理掉它的 turn task；None 表示还在队列里
    consumed_by: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))

    # 处理掉之后这一行只作记录（内容已随那一轮的消息保存）；查询只看还在队列里的，用部分索引
    __table_args__ = (
        Index("ix_session_events_queued", "session_id", sqlite_where=consumed_by.is_(None)),
        {"sqlite_autoincrement": True},
    )


class Notification(Base):
    """发给用户的一条通知。通知页与应用角标的未读数是它的展示，Web Push 是它的投递（deliver task 引用它）。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    # 点开通知打开的会话，随会话删除；系统通知（前置检查告警、推送测试）为 None
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    # 打开通知页或它指向的会话即为已读
    unread: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # id 用作「已读到哪一条」的游标，必须单调且与提交顺序一致：AUTOINCREMENT 不复用，单写者（db.Database）让分配顺序即提交顺序
    __table_args__ = {"sqlite_autoincrement": True}


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class StoredFile(Base):
    """对话里出现的一个文件的元数据；内容按 sha256 存在文件目录里（见 muliao.files）。写入后不再改变。"""

    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
