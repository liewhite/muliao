"""历史领域的表：agent 做过的事，一件一行，只追加。

行写入后不能改、不能删：库里的触发器让 UPDATE / DELETE 直接失败（迁移建出），所以历史只增不改，也不随 agent 的删除
而消失（agent_id 和用量一样只是不透明的 id）。全文检索用 FTS5 虚表 `agent_history_fts`（trigram 分词，中文可用），
它不在 ORM 的 metadata 里，由 AFTER INSERT 触发器与主表同步（行不会改或删，所以只需要这一个触发器）。
"""

from datetime import date, datetime

from sqlalchemy import JSON, Date, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from muliao.platform.db import UNMANAGED_TABLES, Base, UTCDateTime, utcnow
from muliao.platform.tables import agent_ref

# FTS5 虚表：列 title、summary、refs，rowid 即 agent_history.id
FTS_TABLE = "agent_history_fts"
UNMANAGED_TABLES.add(FTS_TABLE)


class AgentHistory(Base):
    """agent 做过的一件事：哪天做的、标题、2–5 行摘要（做了什么、结果、重点）、引用、重要程度。"""

    __tablename__ = "agent_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(agent_ref())
    # 工作发生那天，按配置的时区的日历日（不是写入的日子）
    day: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    # 引用的 issue（#12）、会话 id、commit 等，原样保存的字符串列表
    refs: Mapped[list[str]] = mapped_column(JSON)
    # 1 一般，2 重要，3 很重要
    importance: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # 按 agent 与日期取（某天写过没有、按时间倒序列出）
    __table_args__ = (Index("ix_agent_history_agent_day", "agent_id", "day"), {"sqlite_autoincrement": True})
