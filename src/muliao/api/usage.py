"""用量接口：按时间范围汇总 token 用量。"""

from dataclasses import asdict
from datetime import date

from fastapi import APIRouter, Query
from pydantic import AwareDatetime, BaseModel

from muliao.api.deps import C
from muliao.usage.api import UsageBucket, UsageGroup

router = APIRouter()


class UsageOut(BaseModel):
    """一个分组的汇总。没有参与分组的维度为 null。

    input_tokens 是输入总数，**已包含** cache_read_tokens 与 cache_write_tokens（pydantic-ai 的口径），
    展示时是总数与其中两部分的关系，不要相加。
    """

    agent_id: str | None
    model: str | None
    day: date | None
    requests: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int

    @classmethod
    def of(cls, b: UsageBucket) -> "UsageOut":
        return cls(
            agent_id=b.agent_id,
            model=b.model,
            day=b.day,
            **asdict(b.totals),
        )


@router.get("/usage")
async def usage_summary(
    c: C,
    since: AwareDatetime,
    until: AwareDatetime,
    group_by: list[UsageGroup] | None = Query(None),
) -> list[UsageOut]:
    """[since, until) 之间的用量，按给出的维度分组；不给维度就只有一行总计。

    since / until 必须带时区（`2026-03-01T00:00:00Z`）：不带时区的时刻没有确定含义，在边界上拒绝。
    「天」按服务端配置的时区（`timezone`）划分日历日，与客户端所在时区无关。
    """
    return [UsageOut.of(b) for b in await c.usage.summarize(since, until, group_by or [])]
