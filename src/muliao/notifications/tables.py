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
    # 点开通知打开的会话，随会话删除；系统通知（前置检查告警、推送测试）为 None
    session_id: Mapped[str | None] = mapped_column(session_ref(), index=True)
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
