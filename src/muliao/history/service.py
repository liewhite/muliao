"""历史：agent 做过的事，一件一条，只追加；按时间范围、重要程度与关键词检索。

写入方是 agent 自己（工具 history_append，只能写自己的），读取方是 agent（history_search）与界面。行不能改也不能删
（库里的触发器拦住，见 tables），所以这个领域没有事件、没有任务，也不参与删除 agent 的清理。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import column, exists, func, or_, select, table, text

from muliao.errors import InvalidInput
from muliao.history.tables import FTS_TABLE, AgentHistory
from muliao.platform.db import Database, utcnow

# trigram 分词下 FTS5 只能匹配至少 3 个字符的词，更短的词改用 LIKE
TRIGRAM = 3
MAX_TITLE = 200
IMPORTANCE = range(1, 4)
MAX_LIMIT = 200

_fts = table(FTS_TABLE, column("rowid"))


@dataclass(frozen=True)
class HistoryEntry:
    """要写入的一件事。"""

    # 工作发生的那天（配置时区的日历日）
    day: date
    title: str
    # 2–5 行：做了什么、结果、重点
    summary: str
    # 引用：#issue、会话 id、commit 等
    refs: tuple[str, ...]
    # 1 一般，2 重要，3 很重要
    importance: int


@dataclass(frozen=True)
class HistoryRecord:
    """库里的一件事。"""

    id: int
    agent_id: str
    day: date
    title: str
    summary: str
    refs: tuple[str, ...]
    importance: int
    created_at: datetime


def _record(row: AgentHistory) -> HistoryRecord:
    return HistoryRecord(
        id=row.id,
        agent_id=row.agent_id,
        day=row.day,
        title=row.title,
        summary=row.summary,
        refs=tuple(row.refs),
        importance=row.importance,
        created_at=row.created_at,
    )


def check_entry(entry: HistoryEntry, today: date) -> None:
    """一条要写入的事是否合乎契约；不合乎报 InvalidInput，带上是哪条、哪里不对。"""
    where = f"entry for {entry.day} {entry.title!r}"
    if not entry.title.strip():
        raise InvalidInput(f"{where}: title is empty")
    if len(entry.title) > MAX_TITLE:
        raise InvalidInput(f"{where}: title is longer than {MAX_TITLE} characters")
    if not entry.summary.strip():
        raise InvalidInput(f"{where}: summary is empty")
    if entry.importance not in IMPORTANCE:
        raise InvalidInput(f"{where}: importance must be 1, 2 or 3, got {entry.importance}")
    if entry.day > today:
        raise InvalidInput(f"{where}: day is after today ({today}); record work that already happened")


def match_expression(terms: Sequence[str]) -> str:
    """FTS5 的 MATCH 表达式：每个词作为一个短语（双引号转义），词之间是 AND。"""
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


class HistoryLog:
    """历史的追加与检索。"""

    def __init__(self, db: Database, tz: ZoneInfo, now: Callable[[], datetime] = utcnow):
        self._db = db
        self._tz = tz
        self._now = now

    def today(self) -> date:
        return self._now().astimezone(self._tz).date()

    async def append(self, agent_id: str, entries: Sequence[HistoryEntry]) -> list[HistoryRecord]:
        """给 agent 追加若干件事（一个事务，全部合法才写入）。"""
        if not entries:
            raise InvalidInput("no entries to append")
        today = self.today()
        for entry in entries:
            check_entry(entry, today)
        rows = [
            AgentHistory(
                agent_id=agent_id,
                day=e.day,
                title=e.title.strip(),
                summary=e.summary.strip(),
                refs=list(e.refs),
                importance=e.importance,
                created_at=self._now(),
            )
            for e in entries
        ]
        async with self._db.transaction() as s:
            s.add_all(rows)
            await s.flush()
            return [_record(r) for r in rows]

    async def search(
        self,
        *,
        agent_ids: Sequence[str] | None,
        query: str | None = None,
        since: date | None = None,
        until: date | None = None,
        min_importance: int = 1,
        limit: int = 20,
    ) -> list[HistoryRecord]:
        """检索历史。agent_ids 为 None 表示全部 agent；since / until 是含两端的日期。

        没给 query 按日期倒序（同一天按写入倒序）；给了 query 按空白切词、每个词都要出现（标题、摘要或引用里）：
        词都不短于 3 个字符时用 FTS5（trigram）并按 bm25 相关度排序，否则逐词 LIKE、按日期倒序。"""
        if not 1 <= limit <= MAX_LIMIT:
            raise InvalidInput(f"limit must be between 1 and {MAX_LIMIT}, got {limit}")
        if min_importance not in IMPORTANCE:
            raise InvalidInput(f"min_importance must be 1, 2 or 3, got {min_importance}")
        if since is not None and until is not None and since > until:
            raise InvalidInput(f"since ({since}) is after until ({until})")
        terms = None if query is None else query.split()
        if terms is not None and not terms:
            raise InvalidInput("query is empty; omit it to list by date")
        stmt = select(AgentHistory).where(AgentHistory.importance >= min_importance)
        if agent_ids is not None:
            stmt = stmt.where(AgentHistory.agent_id.in_(agent_ids))
        if since is not None:
            stmt = stmt.where(AgentHistory.day >= since)
        if until is not None:
            stmt = stmt.where(AgentHistory.day <= until)
        by_date = (AgentHistory.day.desc(), AgentHistory.id.desc())
        if terms is None:
            stmt = stmt.order_by(*by_date)
        elif all(len(t) >= TRIGRAM for t in terms):
            stmt = (
                stmt.join(_fts, _fts.c.rowid == AgentHistory.id)
                .where(text(f"{FTS_TABLE} MATCH :match").bindparams(match=match_expression(terms)))
                .order_by(text(f"bm25({FTS_TABLE})"), *by_date)
            )
        else:
            for t in terms:
                # 引用按 JSON 解码后的值匹配（原文里中文被转义、还有括号引号）
                ref = func.json_each(AgentHistory.refs).table_valued("value")
                stmt = stmt.where(
                    or_(
                        AgentHistory.title.contains(t, autoescape=True),
                        AgentHistory.summary.contains(t, autoescape=True),
                        exists(select(1).select_from(ref).where(ref.c.value.contains(t, autoescape=True))),
                    )
                )
            stmt = stmt.order_by(*by_date)
        async with self._db.transaction() as s:
            rows = (await s.scalars(stmt.limit(limit))).all()
            return [_record(r) for r in rows]
