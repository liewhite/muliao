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
    """一次运行的回答交给谁，运行开始时由它处理的信件决定（见 docs/architecture.md「会话领域」），决定开放哪些工具与
    指令里怎么说。USER：有用户在等（运行结束时推送）；AGENT：私有 thread 的运行，回答作为 ask_agent 的结果交回发问方；
    NONE：没有人在等。
    """

    USER = "user"
    AGENT = "agent"
    NONE = "none"


class TaskKind(StrEnum):
    RUN = "run"
    REFLECTION = "reflection"
    DELIVER = "deliver"
    COMPACTION = "compaction"


class JobStatus(StrEnum):
    """后台任务（runtime.jobs）的状态。结束的三种：自己退出（看 exit_code）、被 job_kill 结束、因服务重启被中断。"""

    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"
    INTERRUPTED = "interrupted"


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
    # 两次反思之间的步数（模型回复数，见 runtime.reflection.ReflectionScheduler）
    reflect_every: Mapped[int]
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
    """触发器：具名、持久的事件来源，把外部刺激变成投进某个会话信箱的一封信件。

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


class Job(Base):
    """agent 在后台跑的一条 shell 命令：不占住运行，结束时、运行中每隔一段时间往发起它的会话的信箱投事件（runtime.jobs）。"""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(_fk_agent(), index=True)
    command: Mapped[str] = mapped_column(Text)
    # 注入的 vault key（不存值）
    secrets: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[JobStatus] = mapped_column(StrEnumType(JobStatus), default=JobStatus.RUNNING, index=True)
    exit_code: Mapped[int | None]
    # 输出日志，相对 agent 工作目录（agent 用 read_file 读）
    log_path: Mapped[str] = mapped_column(String(255))
    # 进程（组）号；进程还没起来时为 None
    pid: Mapped[int | None]
    # 发起它的那次运行里用户是否在等回答：完成通知的信件随它（运行中的检查不推送）
    awaited: Mapped[bool]
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = {"sqlite_autoincrement": True}


class MailKind(StrEnum):
    """信箱里的信件（runtime.mailbox）。"""

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
    """会话信箱里的一封信件：一切要交给 agent 的东西都是信件，消费者按到达顺序交给模型（runtime.mailbox）。"""

    __tablename__ = "mail"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    kind: Mapped[MailKind] = mapped_column(StrEnumType(MailKind))
    # 发送方：事件的来源（如 job:12、thread:<会话 id>，交给模型时标在事件上）；用户消息为 None
    source: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    # 随消息发送的文件 id（muliao.files）
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)
    # ANSWER：tool_call_id -> 那次调用的结果（runtime.pending.CallResult 的 dump）
    answers: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 用户是否在等它的回答：一次运行处理过的信件里有一封是，结束时就把回答推送给用户
    awaited: Mapped[bool] = mapped_column(default=False)
    # 委派来的任务里要交回结局的（ask_agent、delegate_task(report_back)）：处理它的运行结束时按这个委派交回
    # （runtime.delegations）。发起会话删除时委派随之删除，这里置空，结局无处可交
    delegation_id: Mapped[int | None] = mapped_column(ForeignKey("delegations.id", ondelete="SET NULL"))
    state: Mapped[MailState] = mapped_column(StrEnumType(MailState), default=MailState.QUEUED)
    # 处理掉它的运行（turn task）
    consumed_by: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # 查询几乎只看还在排队的，用部分索引
    __table_args__ = (
        Index("ix_mail_queued", "session_id", sqlite_where=state == MailState.QUEUED),
        {"sqlite_autoincrement": True},
    )


class DelegationStatus(StrEnum):
    RUNNING = "running"
    # 结局已经交回发起方（完成、被停止、最终失败、子会话被删除）
    SETTLED = "settled"
    # 发起方被用户停止：不再交回
    ABANDONED = "abandoned"


class Delegation(Base):
    """会话交给另一个 agent、要交回结局的一项工作（runtime.delegations）：ask_agent 等它作为挂起调用的结果，
    delegate_task(report_back) 等它作为事件。结局只交回一次（状态从 running 离开）。"""

    __tablename__ = "delegations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 发起委派的会话：结局交回这里，随它删除
    owner_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    # 做这项工作的 thread；它被删除前委派先交回「被删除」（conversation.delete_sessions）
    child_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    # ask_agent 的那次调用：发起方还停在它上面时，结局作为它的结果交回；否则（或 report_back）作为事件
    call_id: Mapped[str | None] = mapped_column(String(100))
    # 发起委派时用户是否在等发起方的回答：交回结局的信件随它推送
    awaited: Mapped[bool]
    status: Mapped[DelegationStatus] = mapped_column(
        StrEnumType(DelegationStatus), default=DelegationStatus.RUNNING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (
        # 同一次 ask_agent 调用只委派一次：续跑重新执行这次调用时据此不重复投信
        Index("ux_delegations_call", "owner_session_id", "call_id", unique=True),
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
