"""会话领域发布的运行事件只带事实（归属表第 9 行），推送规则在通知领域。"""

from typing import get_args

import pytest

from muliao.notifications.inbox import _worth_pushing
from muliao.sessions import pending
from muliao.sessions.events import RunEnded, Waiting
from muliao.sessions.origins import DelegationOrigin, IssueOrigin, TriggerOrigin, UserOrigin

_BASE = {
    "session_id": "s",
    "session_title": "t",
    "agent_id": "a",
    "agent_name": "alpha",
    "run_id": 1,
    "answer": "done",
    "stopped": False,
}


def _ended(waiting_on: list[str], *, awaited: bool = True, stopped: bool = False) -> RunEnded:
    return RunEnded.model_validate(
        {
            **_BASE,
            "origin": {"kind": "user"},
            "waiting_on": waiting_on,
            "awaited": awaited,
            "stopped": stopped,
        }
    )


def test_waiting_kinds_are_the_deferred_kind_names():
    assert set(get_args(Waiting.__value__)) == {k.name for k in pending.KINDS}


def test_origins_round_trip_through_the_event_payload():
    for origin in (UserOrigin(), DelegationOrigin(), TriggerOrigin(trigger_id=3), IssueOrigin(issue_id=4)):
        event = RunEnded.model_validate({**_BASE, "origin": origin, "waiting_on": [], "awaited": False})
        assert RunEnded.model_validate_json(event.model_dump_json()).origin == origin


@pytest.mark.parametrize(
    ("waiting_on", "awaited", "stopped", "pushed"),
    [
        ([], True, False, True),  # 做完了，用户在等
        ([], False, False, False),  # 没人在等
        ([], True, True, False),  # 用户自己停的
        (["ask_user"], True, False, True),  # 停下来问用户
        (["ask_agent"], True, False, False),  # 只在等别的 agent
        (["ask_agent", "ask_user"], True, False, True),
        (["approval"], False, False, True),  # 等批准：没人在等也推
        (["approval"], True, True, True),
    ],
)
def test_push_rule(waiting_on, awaited, stopped, pushed):
    assert _worth_pushing(_ended(waiting_on, awaited=awaited, stopped=stopped)) == pushed
