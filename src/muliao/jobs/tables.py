"""后台任务的表。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref, session_ref


class JobStatus(StrEnum):
    """后台任务（jobs.runner）的状态。结束的三种：自己退出（看 exit_code）、被 job_kill 结束、因服务重启被中断。"""

    RUNNING = "running"
    EXITED = "exited"
    KILLED = "killed"
    INTERRUPTED = "interrupted"


class Job(Base):
    """agent 在后台跑的一条 shell 命令：不占住运行，结束时、运行中每隔一段时间往发起它的会话的信箱投事件（jobs.runner）。"""

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
