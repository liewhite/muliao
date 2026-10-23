"""历史的工具（每个 agent 总是可用）：追加自己做过的事、检索自己或别的 agent 的历史。agent 按名称指代，不出现 id。"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import AgentService
from muliao.history.service import HistoryEntry, HistoryLog
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

# history_search 的 agent 取这个值时检索全部 agent
ALL_AGENTS = "all"


class HistoryItem(BaseModel):
    # history_append 的一件事（类 docstring 会进 schema 给模型看，所以用注释）
    model_config = ConfigDict(extra="forbid")

    day: date = Field(description="Local calendar date (YYYY-MM-DD) the work happened, not today's date.")
    title: str = Field(description="One-line title of this piece of work.")
    summary: str = Field(description="2-5 lines: what was done, the outcome, what matters for later.")
    refs: list[str] = Field(
        default_factory=list, description="References such as `#155`, a session id or a commit hash."
    )
    importance: int = Field(
        default=1, description="1 routine, 2 notable, 3 major (decision, incident, milestone)."
    )


class HistoryFound(BaseModel):
    id: int
    day: date
    # agent 名；已删除的 agent 显示为 (deleted agent <id>)
    agent: str
    title: str
    summary: str
    refs: list[str]
    importance: int


def history_toolset(history: HistoryLog, agents: AgentService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def history_append(ctx: RunContext[RunScope], entries: list[HistoryItem]) -> str:
        """Append entries to your own work history: one entry per piece of work. Entries can never be changed or
        deleted, so check first with `history_search` (since/until = that day) that the day is not recorded yet.

        Args:
            entries: The pieces of work to record.
        """
        records = await history.append(
            ctx.deps.agent_id,
            [HistoryEntry(e.day, e.title, e.summary, tuple(e.refs), e.importance) for e in entries],
        )
        days = sorted({r.day.isoformat() for r in records})
        return f"appended {len(records)} entries (ids {records[0].id}-{records[-1].id}) for {', '.join(days)}"

    @ts.tool
    async def history_search(
        ctx: RunContext[RunScope],
        query: str | None = None,
        since: date | None = None,
        until: date | None = None,
        agent: str | None = None,
        min_importance: int = 1,
        limit: int = 20,
    ) -> list[HistoryFound]:
        """Search the work history (what was done, when, with which issues/commits). Without `query`, entries are
        listed newest day first; with `query`, every whitespace-separated word must appear in the title, summary
        or refs, best matches first. For a vaguer question, list a date range and judge the entries yourself.

        Args:
            query: Keywords, e.g. `trigger precheck` or `#155`.
            since: First day to include (YYYY-MM-DD).
            until: Last day to include (YYYY-MM-DD).
            agent: Agent name, or `all` for every agent; your own history when omitted.
            min_importance: Only entries at least this important (1-3).
            limit: Maximum number of entries (1-200).
        """
        if agent is None:
            agent_ids: list[str] | None = [ctx.deps.agent_id]
        elif agent == ALL_AGENTS:
            agent_ids = None
        else:
            agent_ids = [(await agents.find_by_name(agent)).id]
        found = await history.search(
            agent_ids=agent_ids,
            query=query,
            since=since,
            until=until,
            min_importance=min_importance,
            limit=limit,
        )
        names = await agents.names()
        return [
            HistoryFound(
                id=r.id,
                day=r.day,
                agent=names[r.agent_id],
                title=r.title,
                summary=r.summary,
                refs=list(r.refs),
                importance=r.importance,
            )
            for r in found
        ]

    return ts
