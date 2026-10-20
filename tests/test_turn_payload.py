"""turn task 的 payload 只经过 TurnPayload 读写：写入的形状与读出的一致，形状不对在读取处直接报错。"""

import pytest
from pydantic import ValidationError

from muliao.models import ReplyTo, Task, TaskKind
from muliao.runtime.conversation import ReportTo, TurnPayload
from muliao.runtime.questions import AskResult


def _task(kind: TaskKind, payload: dict) -> Task:
    return Task(id=1, kind=kind, lane="l", payload=payload)


def test_a_dumped_payload_reads_back_the_same():
    payload = TurnPayload(
        prompt="hi",
        reply_to=ReplyTo.USER,
        attachments=["f1"],
        answers={"q1": AskResult(answered=False, answers=[], note="skipped")},
        report_to=ReportTo(session_id="parent", reply_to=ReplyTo.USER),
    )
    assert TurnPayload.of(_task(TaskKind.TURN, payload.dump())) == payload


def test_unknown_or_missing_fields_and_non_turn_tasks_fail_where_they_are_read():
    good = {"prompt": "hi", "reply_to": "user", "attachments": [], "answers": {}, "report_to": None}
    with pytest.raises(ValidationError):
        TurnPayload.of(_task(TaskKind.TURN, {**good, "extra": 1}))
    with pytest.raises(ValidationError):
        TurnPayload.of(_task(TaskKind.TURN, {"prompt": "hi", "reply_to": "user"}))
    with pytest.raises(RuntimeError, match="not a turn"):
        TurnPayload.of(_task(TaskKind.REFLECTION, good))
