"""委派领域的表。"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, StrEnumType, UTCDateTime, utcnow


class DelegationStatus(StrEnum):
    RUNNING = "running"
    # 结局已经交回发起方（完成、被停止、最终失败、子会话被删除）
    SETTLED = "settled"
    # 发起方被用户停止：不再交回
    ABANDONED = "abandoned"


class Delegation(Base):
    """会话交给另一个 agent、要交回结局的一项工作（delegations.manager，流程管理者）：ask_agent 等它作为挂起调用
    的结果，delegate_task(report_back) 等它作为事件。只由委派的投递 lane 创建和修改；会话 id 是不透明引用（没有
    外键），行永久保留（只进入终态）。"""

    __tablename__ = "delegations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 发起方会话上的占用者，也是 thread 信件的关联 id（sessions.holds）
    holder: Mapped[str] = mapped_column(String(200), unique=True)
    owner_session_id: Mapped[str] = mapped_column(String(36), index=True)
    child_session_id: Mapped[str] = mapped_column(String(36), index=True)
    # ask_agent 的那次调用；report_back 时为 None
    call_id: Mapped[str | None] = mapped_column(String(100))
    # 发起委派时用户是否在等发起方的回答：交回结局的信件随它推送
    awaited: Mapped[bool]
    status: Mapped[DelegationStatus] = mapped_column(
        StrEnumType(DelegationStatus), default=DelegationStatus.RUNNING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = {"sqlite_autoincrement": True}
