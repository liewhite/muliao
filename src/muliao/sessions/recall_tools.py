"""session_read：读一个会话的时间线（热历史，sessions.recall）。活动概览 activity_overview 在 history 领域（它还要列出
当天的工作历史）。"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import AgentService
from muliao.errors import InvalidInput
from muliao.platform.text_edit import truncate
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.sessions.info import SessionInfo
from muliao.sessions.origins import Origin
from muliao.sessions.recall import Entry, SessionRecall

SESSION_READ = "session_read"
# 工具结果、事件、工具调用参数默认截到这么长；用户与助手的文字不截
DEFAULT_CLIP = 2000


def short_time(at: datetime, tz: ZoneInfo) -> str:
    """热历史里给模型看的本地时刻（月-日 时:分）。"""
    return at.astimezone(tz).strftime("%m-%d %H:%M")


def local_since(since: str, tz: ZoneInfo) -> datetime:
    """since 参数：ISO 8601 的日期或时刻，不带时区的按本地时间。"""
    try:
        at = datetime.fromisoformat(since.strip())
    except ValueError:
        raise InvalidInput(
            f'`since` must be an ISO 8601 date or time such as "2026-11-04T09:30", not {since!r}'
        ) from None
    return at if at.tzinfo is not None else at.replace(tzinfo=tz)


# 非用户消息条目标签的首词（用户消息的首词是发送方 Origin 的大写）；_label 与 labels 参数的校验都从这里取
_WORDS = {
    "event": "EVENT",
    "assistant": "ASSISTANT",
    "tool_call": "call",
    "tool_result": "result",
    "tool_error": "error",
}
# labels 参数里每项的首词只能是这些（思考不展示，中途停下的标记不能筛）
LABEL_WORDS = (*(o.value.upper() for o in Origin), *_WORDS.values())


def _label(entry: Entry) -> str | None:
    item = entry.item
    if item.type == "user":
        if entry.sender is None:
            raise RuntimeError("a user entry of a timeline must carry its sender")
        return entry.sender.value.upper()
    if item.type == "event":
        if item.source is None:
            raise RuntimeError("an event item must carry its source")
        return f"{_WORDS['event']} {item.source.raw}" + (" (check-in)" if item.check_in else "")
    if item.type == "assistant":
        return _WORDS["assistant"]
    if item.type in ("tool_call", "tool_result", "tool_error"):
        return f"{_WORDS[item.type]} {item.tool_name}"
    return {"thinking": None, "stopped": "-- run stopped by the user --"}[item.type]


def _labelled(label: str, wanted: tuple[str, ...]) -> bool:
    """标签是 wanted 中某一项本身，或以它加空格开头（区分大小写）："call" 匹配所有工具调用，"call run_shell" 只匹配它。"""
    return any(label == w or label.startswith(w + " ") for w in wanted)


def indented(text: str) -> str:
    """热历史的文字里正文缩进两格，挂在标题行下面。"""
    return "\n".join("  " + line for line in text.splitlines())


def place(session: SessionInfo, parent_session_id: str | None) -> str:
    """会话从哪来：来源、发送方身份（issue 号等），thread 再加上名字与父会话 id 前缀。"""
    where = session.origin.value + (f" #{session.origin_ref}" if session.origin_ref else "")
    if session.thread is not None:
        where += f" thread {session.thread!r} of {(parent_session_id or '(deleted)')[:8]}"
    return where


def recall_toolset(
    recall: SessionRecall, agents: AgentService, tz: ZoneInfo, max_chars: int
) -> FunctionToolset[RunScope]:
    """max_chars：一次交给模型的上限（runtime.tool_output_max_chars），超出时在整条处停下、告诉模型从哪接着读。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool(name=SESSION_READ)
    async def session_read(
        ctx: RunContext[RunScope],
        session: str,
        since: str | None = None,
        grep: str | None = None,
        labels: tuple[str, ...] = (),
        clip: int = DEFAULT_CLIP,
        offset: int = 1,
    ) -> str:
        """Read one conversation's timeline (yours or another agent's), as text in time order: what came in (USER =
        typed by the user in the app; TRIGGER / ISSUE / DELEGATION = sent by the session's starter), events, the
        assistant's text, tool calls and their results. User and assistant text is shown in full; tool calls,
        results and events are clipped. Use it to see exactly what was said and done, e.g. a session found with
        `activity_overview`. Output is limited in size: it stops after a whole entry and a last note in square
        brackets says where to continue (`offset`).

        Args:
            session: Session id or a unique prefix of it (8 characters are usually enough).
            since: Only messages saved from this local date or time on, ISO 8601, e.g. "2026-11-04" or
                "2026-11-04T09:30".
            grep: Only entries whose label or text matches this regular expression (case-insensitive, so `USER`
                also matches `/Users/` paths: use `labels` to pick entries by who or what they are).
            labels: Only entries with one of these labels (case-sensitive; each item matches that label or any
                label starting with it and a space): USER (typed by the user), TRIGGER, ISSUE, DELEGATION, EVENT,
                ASSISTANT, `call`, `result`, `error`, or e.g. `call run_shell`. Combined with `grep`, both must
                match.
            clip: Clip tool calls, tool results and events to this many characters; 0 shows them in full.
            offset: Entry number to start from (1-based, counted after `labels` and `grep`), as given in the
                continue note.
        """
        if clip < 0:
            raise InvalidInput(f"clip must be 0 (full) or more, got {clip}")
        if offset < 1:
            raise InvalidInput(f"offset must be 1 or more, got {offset}")
        try:
            pattern = None if grep is None else re.compile(grep, re.IGNORECASE)
        except re.error as e:
            raise InvalidInput(f"grep is not a valid regular expression: {e}") from None
        for wanted in labels:
            if wanted != wanted.strip() or wanted.split(" ", 1)[0] not in LABEL_WORDS:
                raise InvalidInput(
                    f"labels: {wanted!r} is not a label; a label starts with one of {', '.join(LABEL_WORDS)}"
                )
        timeline = await recall.timeline(session, None if since is None else local_since(since, tz))
        shown: list[str] = []
        for entry in timeline.entries:
            label = _label(entry)
            if label is None:
                continue
            if labels and not _labelled(label, labels):
                continue
            text = entry.item.text
            if pattern is not None and not (pattern.search(label) or pattern.search(text)):
                continue
            if entry.item.type not in ("user", "assistant") and clip:
                text = truncate(text, clip)
            files = "".join(
                f"\n[attachment {a['name']!r} ({a['media_type']})]" for a in entry.item.attachments
            )
            shown.append(f"[{short_time(entry.at, tz)}] {label}\n{indented(text + files)}")
        info = timeline.session
        names = await agents.names()
        head = (
            f"# session {info.id} [{place(info, timeline.parent_session_id)}] {info.title!r}, agent {names[info.agent_id]}: "
            f"{len(shown)} entries"
            + ("" if since is None else f" since {since}")
            + ("" if not labels else f" labelled {', '.join(labels)}")
            + ("" if grep is None else f" matching /{grep}/")
        )
        if offset > max(len(shown), 1):
            raise InvalidInput(f"offset {offset} is past the last entry ({len(shown)})")
        out, size = [head], len(head)
        for number in range(offset, len(shown) + 1):
            block = shown[number - 1]
            if size + len(block) + 2 > max_chars and number > offset:
                out.append(
                    f"[output limit reached at entry {number - 1} of {len(shown)}; continue with offset={number}]"
                )
                break
            out.append(block)
            size += len(block) + 2
        return "\n\n".join(out)

    return ts
