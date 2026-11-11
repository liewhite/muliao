"""事件与命令里的来源换成结构化的（#632，归属表第 8、9 行），只动 tasks.payload，不改表结构：
- session.run_ended、session.run_failed：origin / origin_ref 两个字符串换成 origin 对象（{"kind": "issue", "issue_id": 7}，
  用户、委派只有 kind）；run_ended 的 paused / asked_user / needs_approval 换成 waiting_on（停在哪几类挂起的调用上）。
- session.receive：mail 的 origin / origin_ref 换成 sender 对象。
只改没成功的投递（pending、running、failed：关停时还在执行的启动后复位重做，failed 的可以在任务页人工重试）；做完的不再被
读回，保持原样。

降级反算：origin 对象拆回两个字符串；waiting_on 换回三个布尔（paused = 非空，asked_user = 含 ask_user，needs_approval = 含
approval）；sender 拆回 origin / origin_ref。

Revision ID: a7c2e5d90b14
Revises: f4c1a9d27e83
Create Date: 2026-11-11 21:30:00.000000
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "a7c2e5d90b14"
down_revision: str | None = "f4c1a9d27e83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_IDS = {"trigger": "trigger_id", "issue": "issue_id"}


def _structured(origin: str, ref: str | None) -> dict[str, Any]:
    if origin in _IDS:
        if ref is None or not ref.isdecimal():
            raise RuntimeError(f"a {origin} origin needs a numeric ref, got {ref!r}")
        return {"kind": origin, _IDS[origin]: int(ref)}
    if ref is not None:
        raise RuntimeError(f"a {origin} origin has no ref, got {ref!r}")
    return {"kind": origin}


def _flat(structured: dict[str, Any]) -> tuple[str, str | None]:
    kind = structured["kind"]
    return kind, str(structured[_IDS[kind]]) if kind in _IDS else None


def _up(message: dict[str, Any]) -> None:
    data = message["data"]
    match message["type"]:
        case "session.run_ended" | "session.run_failed":
            data["origin"] = _structured(data["origin"], data.pop("origin_ref"))
            if message["type"] == "session.run_ended":
                paused, asked = data.pop("paused"), data.pop("asked_user")
                approval = data.pop("needs_approval", False)
                waiting = (["ask_user"] if asked else []) + (["approval"] if approval else [])
                data["waiting_on"] = waiting or (["ask_agent"] if paused else [])
        case "session.receive":
            mail = data["mail"]
            mail["sender"] = _structured(mail.pop("origin"), mail.pop("origin_ref"))


def _down(message: dict[str, Any]) -> None:
    data = message["data"]
    match message["type"]:
        case "session.run_ended" | "session.run_failed":
            data["origin"], data["origin_ref"] = _flat(data["origin"])
            if message["type"] == "session.run_ended":
                waiting = data.pop("waiting_on")
                data["paused"] = bool(waiting)
                data["asked_user"] = "ask_user" in waiting
                data["needs_approval"] = "approval" in waiting
        case "session.receive":
            mail = data["mail"]
            mail["origin"], mail["origin_ref"] = _flat(mail.pop("sender"))


def _rewrite(change: Callable[[dict[str, Any]], None]) -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, payload FROM tasks WHERE kind = 'event' AND status IN ('pending', 'running', 'failed') "
            "AND json_extract(payload, '$.type') IN ('session.run_ended', 'session.run_failed', 'session.receive')"
        )
    ).all()
    for task_id, payload in rows:
        message = json.loads(payload)
        change(message)
        conn.execute(
            sa.text("UPDATE tasks SET payload = :p WHERE id = :id"), {"p": json.dumps(message), "id": task_id}
        )


def upgrade() -> None:
    _rewrite(_up)


def downgrade() -> None:
    _rewrite(_down)
