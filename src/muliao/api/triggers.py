"""触发器的通用管理接口；某种触发方式专有的操作在它自己的模块里（如 api/webhook.py）。"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.triggers.precheck import Precheck
from muliao.triggers.service import KEEP
from muliao.triggers.tables import Trigger

router = APIRouter()


class TriggerKindOut(BaseModel):
    name: str
    description: str


class TriggerCreate(BaseModel):
    name: str
    agent_id: str
    kind: str
    prompt: str
    config: dict[str, Any]
    # 触发前的前置检查（见 triggers.precheck）；不给则每次都交给 agent
    precheck: Precheck | None = None


class TriggerPatch(BaseModel):
    """只修改给出的字段。precheck 给出 null 表示去掉前置检查，不出现表示不变。"""

    prompt: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    precheck: Precheck | None = None


class TriggerOut(BaseModel):
    id: int
    name: str
    agent_id: str
    kind: str
    prompt: str
    config: dict[str, Any]
    # 这种触发方式自己给出的运行状态（见 TriggerKind.status），不含凭证
    status: dict[str, Any]
    enabled: bool
    precheck: Precheck | None
    last_checked_at: datetime | None
    last_fired_at: datetime | None
    last_session_id: str | None
    # 被自动停用的原因；最近一次前置检查失败的原因
    disabled_reason: str | None
    precheck_error: str | None


class FiredOut(BaseModel):
    # 前置检查判定这次不触发时为 null
    session_id: str | None


def _out(c: C, t: Trigger) -> TriggerOut:
    return TriggerOut(
        id=t.id,
        name=t.name,
        agent_id=t.agent_id,
        kind=t.kind,
        prompt=t.prompt,
        config=t.config,
        status=c.triggers.kind(t.kind).status(t),
        enabled=t.enabled,
        precheck=None if t.precheck is None else Precheck.model_validate(t.precheck),
        last_checked_at=t.last_checked_at,
        last_fired_at=t.last_fired_at,
        last_session_id=t.last_session_id,
        disabled_reason=t.disabled_reason,
        precheck_error=t.precheck_error,
    )


@router.get("/trigger-kinds")
async def trigger_kinds(c: C) -> list[TriggerKindOut]:
    return [TriggerKindOut(name=k.name, description=k.description) for k in c.triggers.kinds.values()]


@router.get("/triggers")
async def list_triggers(c: C) -> list[TriggerOut]:
    return [_out(c, t) for t in await c.triggers.list()]


@router.post("/triggers", status_code=201)
async def create_trigger(c: C, body: TriggerCreate) -> TriggerOut:
    trigger = await c.triggers.create(
        name=body.name,
        agent_id=body.agent_id,
        kind=body.kind,
        prompt=body.prompt,
        config=body.config,
        precheck=body.precheck,
    )
    return _out(c, trigger)


@router.patch("/triggers/{trigger_id}")
async def update_trigger(c: C, trigger_id: int, body: TriggerPatch) -> TriggerOut:
    trigger = await c.triggers.update(
        trigger_id,
        prompt=body.prompt,
        config=body.config,
        enabled=body.enabled,
        precheck=body.precheck if "precheck" in body.model_fields_set else KEEP,
    )
    return _out(c, trigger)


@router.delete("/triggers/{trigger_id}", status_code=204)
async def delete_trigger(c: C, trigger_id: int) -> None:
    await c.triggers.delete(trigger_id)


@router.post("/triggers/{trigger_id}/run", status_code=202)
async def run_trigger(c: C, trigger_id: int) -> FiredOut:
    """立即触发一次（不带事件内容）。有前置检查时同样先跑检查，可以用来测试整条链路。"""
    session = await c.triggers.fire(trigger_id)
    return FiredOut(session_id=None if session is None else session.id)
