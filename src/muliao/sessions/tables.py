"""会话领域的表：会话、消息、信箱、压缩、占用、委派、到点唤醒、调用开出的 thread。各子域独占自己的表（docs/architecture.md 2.1）。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref
from muliao.sessions.kinds import SessionKind
from muliao.sessions.origins import Origin

# 会话标题的列宽：发送方给的标题过长时由新建会话的入口（inbound.create_session）截断
TITLE_LENGTH = 200


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(agent_ref(), index=True)
    title: Mapped[str] = mapped_column(String(TITLE_LENGTH))
    origin: Mapped[Origin] = mapped_column(StrEnumType(Origin))
    # 具体的发送方（sessions.inbound）：触发器 id、issue id、委派方（父）会话 id；用户开的会话为 None
    origin_ref: Mapped[str | None] = mapped_column(String(64))
    # 委派产生的子会话：父会话与它在父会话内的 thread 名（模型用 agent + thread 名指代子会话）
    parent_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL", name="fk_sessions_parent_session_id"), index=True
    )
    thread: Mapped[str | None] = mapped_column(String(100))
    # 会话的种类决定它的规则（sessions.kinds）；thread 种类只由委派创建（origin 为 delegation）
    kind: Mapped[SessionKind] = mapped_column(StrEnumType(SessionKind))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    __table_args__ = (
        Index("ix_sessions_origin_ref", "origin", "origin_ref"),
        Index(
            "uq_sessions_thread",
            "parent_session_id",
            "agent_id",
            "thread",
            unique=True,
            sqlite_where=parent_session_id.isnot(None),
        ),
    )


class Message(Base):
    """一条 pydantic-ai ModelMessage，`payload` 为其 JSON。id 单调递增。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[int]
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # SQLite 默认按 max(rowid)+1 分配 id，删除尾部行后会复用；AUTOINCREMENT 保证单调
    __table_args__ = {"sqlite_autoincrement": True}


class MessageText(Base):
    """一条消息里可搜索的文字（history.searchable_text 从消息派生，写消息时同一事务写入；会话列表按关键字筛用）。单独成表：
    按关键字扫描时不读消息 JSON 的溢出页，也不匹配 JSON 的键名与结构。没有文字的消息没有这一行。"""

    __tablename__ = "message_texts"

    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True)
    text: Mapped[str] = mapped_column(Text)


class Compaction(Base):
    """session 上下文压缩：id <= through_message_id 的消息由 summary 代替发给模型。原始消息仍保留。"""

    __tablename__ = "compactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    through_message_id: Mapped[int]
    # 写入时会话最后一条消息的 id：在它之前的模型回复报告的 token 数反映的是压缩前的上下文（sessions.compaction）
    at_message_id: Mapped[int]
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MailKind(StrEnum):
    """信箱里的信件（sessions.mailbox）。"""

    # 交给模型的原文：用户在界面发的消息（可带附件）、触发器事件、另一个 agent 委派来的任务
    MESSAGE = "message"
    # 对挂起调用的回答（用户回答 ask_user，被问的 agent 回答 ask_agent）：作为那次调用的结果交回，不是一段新消息
    ANSWER = "answer"
    # 后台任务、交回的结果、运行中检查：套上来源交给模型（conventions.format_event），界面单独显示
    EVENT = "event"


class MailState(StrEnum):
    QUEUED = "queued"
    # 交给模型的那一步已经保存
    CONSUMED = "consumed"
    # 用户停止会话时信箱清空：没交给模型，留着记录
    DISCARDED = "discarded"


class Mail(Base):
    """会话信箱里的一封信件：一切要交给 agent 的东西都是信件，消费者按到达顺序交给模型（sessions.mailbox）。"""

    __tablename__ = "mail"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[MailKind] = mapped_column(StrEnumType(MailKind))
    # 发送方：事件的来源（如 job:12、thread:<会话 id>，交给模型时标在事件上）；用户消息为 None
    source: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    # 事件是运行中检查（mailbox.post_check_in 投的，工作还在进行）：交给模型时记进 RUN_CHECK_INS，界面合并连续的检查
    check_in: Mapped[bool] = mapped_column(default=False, server_default="0")
    # 随消息发送的文件 id（muliao.files.store）
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)
    # ANSWER：tool_call_id -> 那次调用的结果（sessions.pending.CallResult 的 dump）
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 用户是否在等它的回答：一次运行处理过的信件里有一封是，结束时就把回答推送给用户
    awaited: Mapped[bool] = mapped_column(default=False)
    # 不透明的关联 id：发送方要知道这封信件带来的工作什么时候结束（委派来的信件是它的 holder，sessions.holds）。
    # 会话在结束时结清它，结局在同一事务里交回发起方（sessions.settlement、sessions.delegation），每个关联只结清一次
    correlation: Mapped[str | None] = mapped_column(String(200))
    correlation_settled: Mapped[bool] = mapped_column(default=False)
    # 幂等键：带同一个键的投信只投一次（命令按键去重，docs/architecture.md 3.4）
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[MailState] = mapped_column(StrEnumType(MailState), default=MailState.QUEUED)
    # 处理掉它的运行（turn task）
    consumed_by: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # 查询几乎只看还在排队的，用部分索引
    __table_args__ = (
        Index("ix_mail_queued", "session_id", sqlite_where=state == MailState.QUEUED),
        Index(
            "uq_mail_dedupe_key", "session_id", "dedupe_key", unique=True, sqlite_where=dedupe_key.isnot(None)
        ),
        Index("ix_mail_correlation", "correlation", sqlite_where=correlation.isnot(None)),
        # 外键的子表索引：删 tasks 时（保留策略）SQLite 按它查引用，没有它每删一行都扫整张 mail
        Index("ix_mail_consumed_by", "consumed_by", sqlite_where=consumed_by.isnot(None)),
        {"sqlite_autoincrement": True},
    )


class HoldStatus(StrEnum):
    # 工作还在进行
    ACTIVE = "active"
    # 结局已经交回（和一封信件同一事务）
    RELEASED = "released"
    # 工作被放弃（会话被停止，或会话自己放弃，如改期、取消到点唤醒），之后的结局不再投
    CLEARED = "cleared"


class SessionHold(Base):
    """占用：别的领域替这个会话在外面做的一项异步工作（sessions.holds）。行永久保留：带 call_id 的占用是「这次挂起
    调用已经交出去」的永久事实。"""

    __tablename__ = "session_holds"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    # 不透明的占用者，如 job:12、delegation:<会话>:<键>
    holder: Mapped[str] = mapped_column(String(200))
    # 这项工作的结局作为这次挂起调用的结果交回（ask_agent）
    call_id: Mapped[str | None] = mapped_column(String(100))
    # 用户是否在等这项工作的结局：交回结局的信件随它推送（发起这项工作的运行有用户在等）
    awaited: Mapped[bool]
    status: Mapped[HoldStatus] = mapped_column(StrEnumType(HoldStatus), default=HoldStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # 占用者全局唯一（job:<id>、delegation:<发起会话>:<工具调用 id>）：委派交回时按它找到发起方（sessions.delegation）
    __table_args__ = (Index("uq_session_holds_holder", "holder", unique=True),)


class Delegation(Base):
    """一项要交回结局的委派（ask_agent、delegate_task(report_back)）的发起参数（委派子域，sessions.delegations）。它的进行
    状态在发起方的占用上（holder 相同），是否结清在 thread 一侧以 holder 为关联 id 的信件上；这里只记委派自己的事：做它的
    thread、心跳间隔与超时。行永久保留，随发起会话删除。"""

    __tablename__ = "delegations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 发起方占用的占用者，也是 thread 信件的关联 id：delegation:<发起会话>:<工具调用 id>
    holder: Mapped[str] = mapped_column(String(200))
    parent_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    # 做这项委派的 thread；thread 被删除后为 None（那时委派已经以「被删除」结清）
    thread_session_id: Mapped[str | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL", name="fk_delegations_thread_session_id"), index=True
    )
    # 运行中检查的间隔（秒）：并发等待的委派（delegate_task(report_back) 的 heartbeat_seconds）；阻塞的为 None
    check_seconds: Mapped[float | None]
    # 阻塞的委派（ask_agent 的 timeout）到期时停掉做它的 thread，结局以「超时被停」交回；并发的为 None
    deadline: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("uq_delegations_holder", "holder", unique=True),)


class WakeUp(Base):
    """会话自己约的一次到点唤醒（wake_me，到点唤醒子域 sessions.wakeups）：到点时向会话投一封事件信件，它接着做。还在不在
    等看会话上同 holder 的占用（active）；改期、取消、会话被停止都让占用不再 active。行永久保留，随会话删除。"""

    __tablename__ = "wakeups"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 在会话上的占用者：wake:<会话>:<工具调用 id>
    holder: Mapped[str] = mapped_column(String(200))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # 约的时候写给到点的自己的说明，随唤醒的信件交回
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (Index("uq_wakeups_holder", "holder", unique=True),)


class ThreadCall(Base):
    """哪次工具调用开出或续写了哪个 thread（sessions.thread_calls）：在开出/续写 thread 的同一个事务里记下，是界面把
    thread 显示在调用处的唯一来源，与调用有没有结果、结果是谁补的无关。续写同一个 thread 时每次调用各一行；随发起会话
    或 thread 删除。"""

    __tablename__ = "thread_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 发起调用的会话（thread 的父会话）；按它与调用 id 唯一（复合唯一索引以它开头，也是它的外键索引）
    parent_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    call_id: Mapped[str] = mapped_column(String(100))
    thread_session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE", name="fk_thread_calls_thread_session_id"), index=True
    )

    __table_args__ = (Index("uq_thread_calls_call", "parent_session_id", "call_id", unique=True),)
