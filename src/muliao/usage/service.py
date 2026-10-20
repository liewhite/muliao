"""用量：记下每次模型运行花掉的 token，按时间范围汇总。

写入方只有 agent_runtime（它是唯一调用模型的地方）；读取方只有界面。两件事都很简单，所以这个领域没有事件、
没有任务、也不参与别的领域的清理：行只增不改，一直留着。
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import select

from muliao.platform.db import Database
from muliao.usage.tables import ModelUsage, RunKind


@dataclass(frozen=True)
class UsageEntry:
    """一次模型运行的用量。token 口径见 ModelUsage：input_tokens 已包含缓存读写。"""

    agent_id: str
    session_id: str | None
    kind: RunKind
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class UsageGroup(StrEnum):
    """汇总时的分组维度，可以任意组合；没有选中的维度在结果里为 None。"""

    AGENT = "agent"
    MODEL = "model"
    DAY = "day"


@dataclass(frozen=True)
class UsageTotals:
    """一组用量的各项计数之和。input_tokens 已包含缓存读写，不要再相加。"""

    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class UsageBucket:
    """一个分组的汇总。没有选中的维度为 None（例如只按 model 分组时 agent_id 与 day 都是 None）。"""

    agent_id: str | None
    model: str | None
    day: date | None
    totals: UsageTotals


@dataclass(frozen=True)
class _Row:
    """汇总要用到的那几列（不取整行）。"""

    created_at: datetime
    agent_id: str
    model: str
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @property
    def counts(self) -> tuple[int, ...]:
        return (
            self.requests,
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
        )


def _summarize(rows: Iterable[_Row], group_by: Sequence[UsageGroup], tz: ZoneInfo) -> list[UsageBucket]:
    """把若干行按选中的维度相加。纯函数：分组规则只在这里写一次。

    「天」按 tz（配置的时区）里的日历日划分——用量要和人看日历的方式对齐，不按 UTC 日切。
    """
    chosen = set(group_by)
    summed: dict[tuple[str | None, str | None, date | None], tuple[int, ...]] = defaultdict(lambda: (0,) * 5)
    for row in rows:
        key = (
            row.agent_id if UsageGroup.AGENT in chosen else None,
            row.model if UsageGroup.MODEL in chosen else None,
            row.created_at.astimezone(tz).date() if UsageGroup.DAY in chosen else None,
        )
        summed[key] = tuple(a + b for a, b in zip(summed[key], row.counts, strict=True))
    return [
        UsageBucket(agent_id=agent_id, model=model, day=day, totals=UsageTotals(*counts))
        # 维度没选中时取值是 None，不能和 str / date 比较：排序键统一成字符串
        for (agent_id, model, day), counts in sorted(
            summed.items(), key=lambda kv: (str(kv[0][2] or ""), kv[0][0] or "", kv[0][1] or "")
        )
    ]


class UsageLog:
    """用量的写入与汇总。"""

    def __init__(self, db: Database, tz: ZoneInfo):
        self._db = db
        self._tz = tz

    async def record(self, entry: UsageEntry) -> None:
        """记一次模型运行的用量（自己开事务）。"""
        async with self._db.transaction() as s:
            s.add(
                ModelUsage(
                    agent_id=entry.agent_id,
                    session_id=entry.session_id,
                    kind=entry.kind,
                    model=entry.model,
                    requests=entry.requests,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    cache_read_tokens=entry.cache_read_tokens,
                    cache_write_tokens=entry.cache_write_tokens,
                )
            )

    async def summarize(
        self, since: datetime, until: datetime, group_by: Sequence[UsageGroup]
    ) -> list[UsageBucket]:
        """[since, until) 之间的用量，按选中的维度汇总；group_by 为空时只有一个总计。"""
        async with self._db.transaction() as s:
            result = await s.execute(
                select(
                    ModelUsage.created_at,
                    ModelUsage.agent_id,
                    ModelUsage.model,
                    ModelUsage.requests,
                    ModelUsage.input_tokens,
                    ModelUsage.output_tokens,
                    ModelUsage.cache_read_tokens,
                    ModelUsage.cache_write_tokens,
                ).where(ModelUsage.created_at >= since, ModelUsage.created_at < until)
            )
            rows = [_Row(*r) for r in result.all()]
        return _summarize(rows, group_by, self._tz)
