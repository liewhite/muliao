"""后台任务的表。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref, session_ref


class JobStatus(StrEnum):
    """后台任务（workspace.runner）的状态。结束的三种：自己退出（看 exit_code）、被 job_kill 结束、因服务重启被中断。"""

    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"
    INTERRUPTED = "interrupted"


class Job(Base):
    """agent 在后台跑的一条 shell 命令：不占住运行，结束时、运行中每隔一段时间往发起它的会话的信箱投事件（workspace.runner）。"""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(session_ref(), index=True)
    agent_id: Mapped[str] = mapped_column(agent_ref(), index=True)
    command: Mapped[str] = mapped_column(Text)
    # 注入的 vault key（不存值）
    secrets: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[JobStatus] = mapped_column(StrEnumType(JobStatus), default=JobStatus.RUNNING, index=True)
    exit_code: Mapped[int | None]
    # 输出日志，相对 agent 工作目录（agent 用 read_file 读）
    log_path: Mapped[str] = mapped_column(String(255))
    # 进程（组）号；进程还没起来时为 None
    pid: Mapped[int | None]
    # 运行中检查的间隔（秒，run_shell 的 heartbeat_seconds）
    check_seconds: Mapped[float]
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = {"sqlite_autoincrement": True}


class ScriptGrant(Base):
    """用户对一个 skill 脚本调用的「始终允许」（#531，workspace.grants）：之后同一个 agent 的同一个调用（同一 skill、
    脚本、参数、secret，且 skill 目录里除 SKILL.md 外的内容没变）不再停下等批准。存库不放资源目录：agent 能写自己的
    资源目录，等于自己批准自己。用户在设置页查看、撤销（删行）；agent 删除时一起删。"""

    __tablename__ = "script_grants"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(agent_ref(), index=True)
    skill: Mapped[str] = mapped_column(String(100))
    # 脚本相对 skill 目录的路径（解析后的）
    script: Mapped[str] = mapped_column(Text)
    args: Mapped[list[str]] = mapped_column(JSON)
    # 注入的 vault key（排序去重，不存值）
    secrets: Mapped[list[str]] = mapped_column(JSON)
    # skill 目录除 SKILL.md 外的内容指纹（agents.SkillScripts.fingerprint）
    fingerprint: Mapped[str] = mapped_column(String(64))
    # 以上各项（含 agent_id）合起来的摘要：查找与去重按它（workspace.grants.ScriptCall.key）
    key: Mapped[str] = mapped_column(String(64), unique=True)
    # 用户点「始终允许」的那次调用：所在会话与调用 id（只作来历，会话删了也留着）
    session_id: Mapped[str] = mapped_column(session_ref())
    call_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # 按它直接放行的次数与最后一次的时间
    hits: Mapped[int] = mapped_column(default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = {"sqlite_autoincrement": True}
