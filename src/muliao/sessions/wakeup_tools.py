"""wake_me：会话约自己到点被唤醒（sessions.wakeups）。"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic_ai import FunctionToolset, RunContext

from muliao.errors import InvalidInput
from muliao.platform.db import Database, utcnow
from muliao.platform.tasks import Clock
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.sessions.wakeups import WAKE_ME, cancel_in, local_time, schedule_in


def _due(at: str | None, after_seconds: float | None, tz: ZoneInfo, now: datetime) -> datetime:
    if (at is None) == (after_seconds is None):
        raise InvalidInput("give exactly one of `at` and `after_seconds` (or `cancel: true` alone)")
    if after_seconds is not None:
        if not after_seconds > 0:  # 也拒绝 NaN
            raise InvalidInput("after_seconds must be greater than 0")
        try:
            return now + timedelta(seconds=after_seconds)
        except OverflowError:
            raise InvalidInput(f"after_seconds {after_seconds} is too far in the future") from None
    assert at is not None
    try:
        due = datetime.fromisoformat(at.strip())
    except ValueError:
        raise InvalidInput(f'`at` must be an ISO 8601 time such as "2026-10-22T10:30", not {at!r}') from None
    # 不带时区的按服务端时区解释（用户与 agent 说的都是本地时间），同 once 触发器
    if due.tzinfo is None:
        due = due.replace(tzinfo=tz)
    if due <= now:
        raise InvalidInput(f"`at` {at!r} is in the past (now is {local_time(now, tz)})")
    return due


def wakeup_toolset(db: Database, tz: ZoneInfo, clock: Clock = utcnow) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool(name=WAKE_ME)
    async def wake_me(
        ctx: RunContext[RunScope],
        note: str | None = None,
        at: str | None = None,
        after_seconds: float | None = None,
        cancel: bool = False,
    ) -> str:
        """Wake this conversation up later: when the time comes, your `note` arrives here as an event and you
        continue. Until then the conversation is waiting (like on a background job or delegated work): it is not
        idle, so an issue it works on is not stalled and you are not asked to settle it. Use it when you can only
        check now and then (a deploy settling, a reply on an external platform, a result due later); when a
        script can poll for the condition, run the polling loop with `run_shell(background: true)` instead, so
        it ends as soon as the condition is met. A conversation has at most one wake-up: scheduling again
        replaces it, `cancel: true` removes it, and the user stopping the conversation cancels it. After
        scheduling, end your turn.

        Args:
            note: Required when scheduling: what to do when woken (you will see only this note then).
            at: When to wake up, ISO 8601, e.g. "2026-10-22T10:30"; without an offset it is local time.
            after_seconds: Instead of `at`: seconds from now (> 0, no upper limit).
            cancel: Remove the scheduled wake-up instead of scheduling one (give nothing else).
        """
        session_id = ctx.deps.session_id
        if cancel:
            if note is not None or at is not None or after_seconds is not None:
                raise InvalidInput("`cancel: true` takes no other arguments")
            async with db.transaction() as s:
                cancelled = await cancel_in(s, session_id)
            if cancelled is None:
                return "no wake-up was scheduled; nothing to cancel"
            return f"cancelled wake-up at {local_time(cancelled, tz)}"
        now = clock()
        due = _due(at, after_seconds, tz, now)
        if note is None or not note.strip():
            raise InvalidInput("`note` is required: say what to do when woken")
        if ctx.tool_call_id is None:
            raise RuntimeError("wake_me was called without a tool call id")
        async with db.transaction() as s:
            scheduled = await schedule_in(
                s, session_id, ctx.tool_call_id, due, note, awaited=ctx.deps.user_waits
            )
        text = f"scheduled wake-up at {local_time(scheduled.due_at, tz)}"
        if scheduled.replaced is not None:
            text += f"; replaced the one at {local_time(scheduled.replaced, tz)}"
        return text

    return ts
