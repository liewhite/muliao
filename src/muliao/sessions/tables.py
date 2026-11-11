"""会话领域的表：会话、消息、信箱、压缩、占用、委派、到点唤醒、调用开出的 thread。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref
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
    # 索引：按日查各会话的活动（sessions.recall）
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

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
    # 经来信入口（sessions.inbound）投进来的信件的发送方来源：消息一定有，用户对提问的回答也有（user）；事件与交回的
    # 结局（sessions.work）为 None。「用户说了什么」只按它认（sessions.recall）。迁移前的行按会话与内容回填
    origin: Mapped[Origin | None] = mapped_column(StrEnumType(Origin))
    content: Mapped[str] = mapped_column(Text)
    # 事件是运行中检查（work.post_check_in 投的，工作还在进行）：交给模型时记进 RUN_CHECK_INS，界面合并连续的检查
    check_in: Mapped[bool] = mapped_column(default=False, server_default="0")
    # 随消息发送的文件 id（muliao.files.store）
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)
    # ANSWER：tool_call_id -> 那次调用的结果（sessions.pending.CallResult 的 dump）
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 用户是否在等它的回答：一次运行处理过的信件里有一封是，结束时就把回答推送给用户
    awaited: Mapped[bool] = mapped_column(default=False)
    # 委派来的信件：是哪项委派（thread_calls 行）。会话做完时结清它，结局在
    # 同一事务里交回发起方（sessions.settlement、sessions.delegation）；结清没有只看发起方的占用是否还 active。
    # 发起会话删除时行随之删除，这里置空（结局没有收件人）
    delegation_id: Mapped[int | None] = mapped_column(
        ForeignKey("thread_calls.id", ondelete="SET NULL", name="fk_mail_delegation_id")
    )
    # 幂等键：带同一个键的投信只投一次（命令按键去重，docs/architecture.md 3.4）
    dedupe_key: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[MailState] = mapped_column(StrEnumType(MailState), default=MailState.QUEUED)
    # 交给了哪次运行（只经 sessions.mailbox.give_in / consume_in 写）：投递运行时信箱里已有的、
    # 运行开头取走的、交给模型那一步保存的（这时 state 记为 consumed）。排着的信件之后被更新的运行取走时改指那次运行
    taken_by: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    # 索引：按日查各会话收到的信件（sessions.recall）
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    # 查询几乎只看还在排队的，用部分索引
    __table_args__ = (
        Index("ix_mail_queued", "session_id", sqlite_where=state == MailState.QUEUED),
        Index(
            "uq_mail_dedupe_key", "session_id", "dedupe_key", unique=True, sqlite_where=dedupe_key.isnot(None)
        ),
        Index("ix_mail_delegation_id", "delegation_id", sqlite_where=delegation_id.isnot(None)),
        # 外键的子表索引：删 tasks 时（保留策略）SQLite 按它查引用，没有它每删一行都扫整张 mail
        Index("ix_mail_taken_by", "taken_by", sqlite_where=taken_by.isnot(None)),
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
    # 外部工作的不透明占用者，如 job:12、wake:<会话>:<键>；委派的占用为 None，引用委派行（thread_call_id）
    holder: Mapped[str | None] = mapped_column(String(200))
    # 委派的占用：引用那项委派（thread_calls 行，委派的唯一主体）。thread 删除时行随之删除，这里置空（那时占用已经
    # 结清，只剩 call_id 这一永久事实）
    thread_call_id: Mapped[int | None] = mapped_column(
        ForeignKey("thread_calls.id", ondelete="SET NULL", name="fk_session_holds_thread_call_id")
    )
    # 这项工作的结局作为这次挂起调用的结果交回（ask_agent）
    call_id: Mapped[str | None] = mapped_column(String(100))
    # 用户是否在等这项工作的结局：交回结局的信件随它推送（发起这项工作的运行有用户在等）
    awaited: Mapped[bool]
    status: Mapped[HoldStatus] = mapped_column(StrEnumType(HoldStatus), default=HoldStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        # 外部工作的占用者全局唯一（job:<id>、wake:<会话>:<键>）：结局按它找到占用
        Index("uq_session_holds_holder", "holder", unique=True),
        Index(
            "uq_session_holds_thread_call_id",
            "thread_call_id",
            unique=True,
            sqlite_where=thread_call_id.isnot(None),
        ),
    )


class WakeUp(Base):
    """会话自己约的一次到点唤醒（wake_me，sessions.wakeups）：到点时向会话投一封事件信件，它接着做。还在不在
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


class ApprovalRequest(Base):
    """这次工具调用停下来等用户批准了（sessions.approvals）：运行停在等批准的调用上时，在它收尾的事务里记下。是「没有
    结果的调用在等批准」与「执行到一半被打断」的区分依据（sessions.pending）。行永久保留，随会话删除。"""

    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    call_id: Mapped[str] = mapped_column(String(100))
    # 工具请求批准时定下的被批准对象的指纹（agent_runtime.require_approval 的 pin，如 skill 脚本内容），批准时交回给工具比对；
    # #570 之前的行为空（工具当作对不上）
    pin: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 批准第一次交给运行去执行的时间（sessions.approvals.take_in）：之后再交就是重复执行，改给「被中断」的结果
    taken_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # 复合唯一索引以 session_id 开头，也是它的外键索引
    __table_args__ = (Index("uq_approval_requests_call", "session_id", "call_id", unique=True),)


class ThreadCall(Base):
    """哪次工具调用开出或续写了哪个 thread（sessions.thread_calls）：在开出/续写 thread 的同一个事务里记下，是界面把
    thread 显示在调用处的唯一来源，与调用有没有结果、结果是谁补的无关。续写同一个 thread 时每次调用各一行；随发起会话
    或 thread 删除。要交回结局的调用（ask_agent、delegate_task(report_back)）就是一项委派，发起方有一个引用这一行的占用，
    行上再记心跳间隔（#546 前另存在 delegations 表）：行是委派的唯一主体，发起方的占用
    （session_holds.thread_call_id）与 thread 的信件（mail.delegation_id）都只引用它的 id；结清没有看占用是否还
    active。"""

    __tablename__ = "thread_calls"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 发起调用的会话（thread 的父会话）；按它与调用 id 唯一（复合唯一索引以它开头，也是它的外键索引）
    parent_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    call_id: Mapped[str] = mapped_column(String(100))
    thread_session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE", name="fk_thread_calls_thread_session_id"), index=True
    )
    # 并发等待的委派（delegate_task(report_back) 的 heartbeat_seconds）的运行中检查间隔（秒）；其余为 None
    check_seconds: Mapped[float | None]

    __table_args__ = (Index("uq_thread_calls_call", "parent_session_id", "call_id", unique=True),)
