"""历史的工具（每个 agent 总是可用）：追加自己做过的事、检索自己或别的 agent 的历史（冷历史），以及某一天的活动概览
（热历史：会话与用户说的话来自会话领域的 SessionRecall，加上当天的工作历史）。agent 按名称指代，不出现 id。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import AgentNames, AgentService
from muliao.history.service import MAX_LIMIT, HistoryEntry, HistoryLog, HistoryRecord
from muliao.issues.api import IssueService, UserActions
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.sessions.api import SESSION_READ, Activity, DaySession, SessionRecall

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


def _hm(at: datetime, tz: ZoneInfo) -> str:
    return at.astimezone(tz).strftime("%H:%M")


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())


def _where(d: DaySession) -> str:
    info = d.session
    where = info.origin.value + (f" #{info.origin_ref}" if info.origin_ref else "")
    if info.thread is not None:
        where += f" thread {info.thread!r} of {(d.parent_session_id or '(deleted)')[:8]}"
    return where


def overview_text(
    activity: Activity,
    actions: list[UserActions],
    records: list[HistoryRecord],
    names: AgentNames,
    who: str,
    day: date,
    tz: ZoneInfo,
) -> str:
    """activity_overview 交给模型的文字：会话、用户在会话里说的话与对 issue 做的事（全文）、失败的运行、当天的工作历史。"""
    many = len({d.session.agent_id for d in activity.sessions} | {r.agent_id for r in records}) > 1

    def agent_of(agent_id: str) -> str:
        return f"{names[agent_id]} " if many else ""

    by_id = {d.session.id: d for d in activity.sessions}
    origins: dict[str, int] = {}
    for d in activity.sessions:
        origins[d.session.origin.value] = origins.get(d.session.origin.value, 0) + 1
    out = [f"# activity of {who} on {day} (local day, times local)", ""]
    out.append(
        f"## sessions ({len(activity.sessions)}"
        + "".join(f", {k} {v}" for k, v in sorted(origins.items()))
        + ")"
    )
    for d in activity.sessions:
        span = (
            "no messages yet"
            if d.span is None
            else f"{d.span.messages} msgs {_hm(d.span.first_at, tz)}-{_hm(d.span.last_at, tz)}"
        )
        failed = f", {len(d.failures)} FAILED" if d.failures else ""
        out.append(
            f"- {agent_of(d.session.agent_id)}{d.session.id[:8]} [{_where(d)}] {d.session.title[:80]!r}: "
            f"{d.state.value}, {span}, {d.runs} runs{failed}"
        )
    if not activity.sessions:
        out.append("(none)")
    out += ["", f"## what the user typed ({len(activity.words)}, full text)"]
    for w in activity.words:
        d = by_id[w.session_id]
        files = f" (+{w.attachments} files)" if w.attachments else ""
        out.append(
            f"- {_hm(w.at, tz)} {agent_of(d.session.agent_id)}{w.session_id[:8]} [{_where(d)}] "
            f"{d.session.title[:40]!r}{files}:\n{_indent(w.text)}"
        )
    if not activity.words:
        out.append("(none)")
    out += ["", f"## what the user did on issues ({len(actions)} issues, comments in full)"]
    for a in actions:
        out.append(f"- #{a.issue_id} {a.title[:80]!r} (now {a.status.value}):")
        out += [_indent(line) for line in a.lines]
    if not actions:
        out.append("(none)")
    failures = [(f, d) for d in activity.sessions for f in d.failures]
    out += ["", f"## failed runs ({len(failures)})"]
    for f, d in sorted(failures, key=lambda x: x[0].at):
        error = " ".join(f.error.split())
        out.append(
            f"- {_hm(f.at, tz)} {agent_of(d.session.agent_id)}{d.session.id[:8]} run {f.run_id}: {error[:300]}"
        )
    if not failures:
        out.append("(none)")
    out += ["", f"## work history for {day} ({len(records)})"]
    out += [f"- {agent_of(r.agent_id)}[{r.importance}] {r.title} {list(r.refs)}" for r in records] or [
        "(none yet)"
    ]
    out += [
        "",
        f"Next: `{SESSION_READ}(session=<id prefix>)` for a session worth a closer look (`since`/`grep` to narrow it); "
        "`issue_show` for an issue's full thread.",
    ]
    return "\n".join(out)


def history_toolset(
    history: HistoryLog, agents: AgentService, recall: SessionRecall, issues: IssueService
) -> FunctionToolset[RunScope]:
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
        """Search the work history (what was done, when, with which issues/commits). Returns a JSON array of
        entries, each with `id`, `day` (YYYY-MM-DD), `agent`, `title`, `summary`, `refs` and `importance`.
        Without `query`, entries are listed newest day first; with `query`, every whitespace-separated word must
        appear in the title, summary or refs, best matches first. For a vaguer question, list a date range and
        judge the entries yourself.

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

    @ts.tool
    async def activity_overview(
        ctx: RunContext[RunScope], day: date | None = None, agent: str | None = None
    ) -> str:
        """What happened on one local calendar day (default today), as text: every conversation that was active
        (id prefix, origin/issue, title, state running/waiting/idle, messages and time span, runs and failures),
        everything the user typed that day and every comment or change the user made on the agent's issues (in
        full), failed runs with their errors, and that day's work-history entries. Use it to recall recent work (e.g. today, which `history_search` does not have yet), then
        `session_read` for a conversation worth a closer look.

        Args:
            day: Local calendar date (YYYY-MM-DD); today when omitted.
            agent: Agent name, or `all` for every agent; yourself when omitted.
        """
        if agent is None:
            agent_ids: list[str] | None = [ctx.deps.agent_id]
        elif agent == ALL_AGENTS:
            agent_ids = None
        else:
            agent_ids = [(await agents.find_by_name(agent)).id]
        names = await agents.names()
        day = history.today() if day is None else day
        start, end = history.day_bounds(day)
        activity = await recall.between(start, end, agent_ids)
        actions = await issues.user_actions_between(start, end, agent_ids)
        records = await history.search(agent_ids=agent_ids, since=day, until=day, limit=MAX_LIMIT)
        who = "every agent" if agent_ids is None else names[agent_ids[0]]
        return overview_text(activity, actions, list(reversed(records)), names, who, day, history.tz)

    return ts
