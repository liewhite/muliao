"""热历史（会话领域的读模型）：一段时间里 agent 的会话做了什么、用户说了什么，以及一个会话的时间线。给热历史的两个
工具用（history 领域的 activity_overview、本领域的 session_read）；冷历史是 agent 自己写的工作历史（history 领域）。

「用户说了什么」只按信件记下的发送方来源认（Mail.origin 为 user：用户在界面上发的消息与对提问的回答），不从文字猜谁发的；
时间线里的用户消息条目按保存它的那次运行处理的用户信件整段对上（_from_user）。
时间线的条目与会话页同一份投影（transcript.to_items），只要文字，不用工具的展示规则。"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime

from muliao.errors import InvalidInput, NotFound
from muliao.platform.db import Database
from muliao.platform.tasks import TaskQueue
from muliao.sessions.activity import WorkState, work_states_in
from muliao.sessions.codec import load_messages
from muliao.sessions.directory import among_in, with_prefix_in
from muliao.sessions.history import Span, spans_between_in, transcript_rows_in
from muliao.sessions.info import SessionInfo, info_of
from muliao.sessions.mailbox import first_arrivals_between_in, sent_by_in
from muliao.sessions.origins import Origin
from muliao.sessions.pending import UserAnswered, call_result
from muliao.sessions.questions import late_answer_lines
from muliao.sessions.runs import failure_of, runs_between_in
from muliao.sessions.tables import Mail, MailKind
from muliao.sessions.transcript import Item, to_items


@dataclass(frozen=True)
class RunFailure:
    """一次最终失败的运行：排上的时刻与错误说明。"""

    run_id: int
    at: datetime
    error: str


@dataclass(frozen=True)
class DaySession:
    """一段时间里有活动（保存了消息或收到了信件）的一个会话。"""

    session: SessionInfo
    # thread 的父会话（发起委派的会话）；顶层会话与父会话已删除的为 None
    parent_session_id: str | None
    # 此刻的工作状态（与会话列表同一条规则）
    state: WorkState
    # 这段时间里保存的消息；只收到了信件、还没保存消息的为 None
    span: Span | None
    # 这段时间里第一次活动（第一封信件到达或第一条消息保存）
    first_at: datetime
    # 这段时间里排上的运行数，与其中最终失败的
    runs: int
    failures: list[RunFailure]


@dataclass(frozen=True)
class UserWords:
    """用户在界面上说的一段话：发的消息（content），或对提问的回答（每题一行）。"""

    session_id: str
    at: datetime
    text: str
    # 随消息发送的文件数
    attachments: int


@dataclass(frozen=True)
class Activity:
    sessions: list[DaySession]
    words: list[UserWords]


@dataclass(frozen=True)
class Entry:
    """时间线的一条：所在消息保存的时刻与展示条目。"""

    at: datetime
    item: Item
    # 用户消息条目（type 为 user）的发送方来源：用户在界面上写的是 user，否则是会话的发起方（别的来源只续写自己发起的
    # 会话）；其他条目为 None
    sender: Origin | None


@dataclass(frozen=True)
class Timeline:
    session: SessionInfo
    parent_session_id: str | None
    entries: list[Entry]


def _words(mail: Mail) -> UserWords:
    if mail.kind == MailKind.ANSWER:
        results = [call_result(v) for v in mail.answers.values()]
        asked = [r.result for r in results if isinstance(r, UserAnswered)]
        text = "\n".join(["(answers to ask_user)", *late_answer_lines(asked)])
    else:
        text = mail.content
    return UserWords(mail.session_id, mail.created_at, text, len(mail.attachments))


def _has_segment(text: str, segment: str) -> bool:
    """条目文字里有这一整段：一条用户消息条目是连续内容段用换行连起来的（transcript._user_item）。"""
    return (
        text == segment
        or text.startswith(segment + "\n")
        or text.endswith("\n" + segment)
        or f"\n{segment}\n" in text
    )


def _from_user(item: Item, typed: Sequence[Mail]) -> bool:
    """用户消息条目里有用户在界面上写的一封信：typed 是保存这条消息的那次运行处理的用户消息（信件交给模型时
    原文作为一整段，sessions.content）；只有附件的按条目带不带附件认。"""
    return any(
        _has_segment(item.text, m.content) if m.content.strip() else bool(item.attachments) for m in typed
    )


class SessionRecall:
    def __init__(self, db: Database, queue: TaskQueue):
        self._db = db
        self._queue = queue

    async def between(self, start: datetime, end: datetime, agent_ids: Collection[str] | None) -> Activity:
        """[start, end) 里这些 agent（None 为全部）有活动的会话，按第一次活动排序；以及用户这段时间说的话，按时间排序。"""
        async with self._db.transaction() as s:
            spans = await spans_between_in(s, start, end)
            arrivals = await first_arrivals_between_in(s, start, end)
            rows = await among_in(s, spans.keys() | arrivals.keys())
            sessions = [x for x in rows.values() if agent_ids is None or x.agent_id in agent_ids]
            ids = [x.id for x in sessions]
            states = await work_states_in(s, self._queue, ids)
            runs = await runs_between_in(s, self._queue, ids, start, end)
            words = await sent_by_in(s, ids, Origin.USER, start=start, end=end)
            found = []
            for x in sessions:
                span = spans.get(x.id)
                times = [t for t in (arrivals.get(x.id), span and span.first_at) if t is not None]
                found.append(
                    DaySession(
                        session=info_of(x),
                        parent_session_id=x.parent_session_id,
                        state=states[x.id],
                        span=span,
                        first_at=min(times),
                        runs=len(runs.get(x.id, [])),
                        failures=[
                            RunFailure(r.id, r.created_at, failure_of(r))
                            for r in runs.get(x.id, [])
                            if r.failed
                        ],
                    )
                )
        found.sort(key=lambda d: (d.first_at, d.session.id))
        return Activity(found, [_words(m) for m in words])

    async def timeline(self, session: str, since: datetime | None) -> Timeline:
        """一个会话（id 或唯一的 id 前缀）的时间线；since 只取这之后保存的消息。"""
        async with self._db.transaction() as s:
            matches = await with_prefix_in(s, session, limit=2)
            if not matches:
                raise NotFound(f"no session id starts with {session!r}")
            if len(matches) > 1:
                raise InvalidInput(
                    f"session id prefix {session!r} matches several sessions; give more characters"
                )
            (row,) = matches
            rows = await transcript_rows_in(s, row.id, since=since)
            typed: dict[int | None, list[Mail]] = {}
            for m in await sent_by_in(s, [row.id], Origin.USER):
                if m.kind == MailKind.MESSAGE:
                    typed.setdefault(m.consumed_by, []).append(m)
            info, parent = info_of(row), row.parent_session_id
        entries = []
        messages = load_messages(p for _, p, _, _ in rows)
        for (_, _, saved_at, run_id), message in zip(rows, messages, strict=True):
            for item in to_items([message], {}, saved_at=saved_at):
                sender = None
                if item.type == "user":
                    sender = Origin.USER if _from_user(item, typed.get(run_id, [])) else info.origin
                entries.append(Entry(saved_at, item, sender))
        return Timeline(info, parent, entries)
