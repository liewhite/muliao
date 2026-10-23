"""通知领域的表。"""

from datetime import datetime

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import Base, UTCDateTime, utcnow
from muliao.platform.tables import session_ref


class Notification(Base):
    """发给用户的一条通知。通知页与应用角标的未读数是它的展示，Web Push 是它的投递（deliver task 引用它）。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    # 通知关于的会话（没有 issue_id 时点开打开它），随会话删除；系统通知（前置检查告警投不到 agent 时的兜底、推送测试）为 None
    session_id: Mapped[str | None] = mapped_column(session_ref(), index=True)
    # 交给用户审核的 issue（Notify.issue_id）：点开打开它而不是会话，打开它即已读。不设外键：issue 属于别的领域，
    # issue 被删后通知照旧指向它（点开看到「不存在」），随会话删除
    issue_id: Mapped[int | None] = mapped_column(index=True)
    # 打开通知页、它关于的会话或 issue 即为已读
    unread: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def url(self) -> str | None:
        """点开通知打开的页面（推送与通知页共用）：交给用户审核的 issue、通知关于的会话；系统通知没有（推送点开看通知页）。"""
        if self.issue_id is not None:
            return f"/issues/{self.issue_id}"
        if self.session_id is not None:
            return f"/sessions/{self.session_id}"
        return None

    # id 用作「已读到哪一条」的游标，必须单调且与提交顺序一致：AUTOINCREMENT 不复用，单写者（db.Database）让分配顺序即提交顺序
    __table_args__ = {"sqlite_autoincrement": True}


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    p256dh: Mapped[str] = mapped_column(Text)
    auth: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
