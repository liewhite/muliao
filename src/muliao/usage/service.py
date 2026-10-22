"""用量：记下每次模型运行花掉的 token，按时间范围汇总。

写入方只有 agent_runtime（它是唯一调用模型的地方）；读取方只有界面。两件事都很简单，所以这个领域没有事件、
没有任务、也不参与别的领域的清理：行只增不改，一直留着。
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from sqlalchemy import select

from muliao.platform.db import Database
from muliao.usage.tables import ModelUsage, RunKind


class UsageGroup(StrEnum):
    """汇总时的分组维度，可以任意组合；没有选中的维度在结果里为 None。"""

    AGENT = "agent"
    MODEL = "model"
    DAY = "day"


@dataclass(frozen=True)
class UsageTotals:
    """用量的各项计数（一次运行的，或一组的和）。input_tokens 已包含缓存读写，不要再相加。

    计数有哪几项只在这里列出：写入、查询、相加都从它的字段推导（COUNTS）。"""

    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @classmethod
    def zero(cls) -> "UsageTotals":
        return cls(*(0 for _ in COUNTS))

    def __add__(self, other: "UsageTotals") -> "UsageTotals":
        return UsageTotals(*(getattr(self, f) + getattr(other, f) for f in COUNTS))


# 计数的字段名（与 ModelUsage 的列同名）
COUNTS = tuple(f.name for f in fields(UsageTotals))


@dataclass(frozen=True)
class UsageEntry:
    """一次模型运行的用量。token 口径见 ModelUsage：input_tokens 已包含缓存读写。"""

    agent_id: str
    session_id: str | None
    kind: RunKind
    model: str
    totals: UsageTotals


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
    totals: UsageTotals


def _summarize(rows: Iterable[_Row], group_by: Sequence[UsageGroup], tz: ZoneInfo) -> list[UsageBucket]:
    """把若干行按选中的维度相加。纯函数：分组规则只在这里写一次。

    「天」按 tz（配置的时区）里的日历日划分——用量要和人看日历的方式对齐，不按 UTC 日切。
    """
    chosen = set(group_by)
    summed: dict[tuple[str | None, str | None, date | None], UsageTotals] = defaultdict(UsageTotals.zero)
    for row in rows:
        key = (
            row.agent_id if UsageGroup.AGENT in chosen else None,
            row.model if UsageGroup.MODEL in chosen else None,
            row.created_at.astimezone(tz).date() if UsageGroup.DAY in chosen else None,
        )
        summed[key] += row.totals
    return [
        UsageBucket(agent_id=agent_id, model=model, day=day, totals=totals)
        # 维度没选中时取值是 None，不能和 str / date 比较：排序键统一成字符串
        for (agent_id, model, day), totals in sorted(
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
                    **asdict(entry.totals),
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
                    *(getattr(ModelUsage, f) for f in COUNTS),
                ).where(ModelUsage.created_at >= since, ModelUsage.created_at < until)
            )
            rows = [_Row(*r[:3], UsageTotals(*r[3:])) for r in result.all()]
        return _summarize(rows, group_by, self._tz)
