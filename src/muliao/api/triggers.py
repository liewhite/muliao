"""触发器的通用管理接口；某种触发方式专有的操作在它自己的模块里（如 api/webhook.py）。"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, TypeAdapter

from muliao.api.deps import C
from muliao.platform.values import KEEP
from muliao.triggers.api import (
    OnceConfig,
    OnceStatus,
    Precheck,
    ScheduleConfig,
    ScheduleStatus,
    WebhookConfig,
    WebhookStatus,
)
from muliao.triggers.tables import Trigger

router = APIRouter()


# 所有触发方式；与 container 里注册的方式一一对应（tests/test_triggers.py 校验）
TriggerKindName = Literal["schedule", "once", "webhook"]


class TriggerKindOut(BaseModel):
    name: TriggerKindName
    description: str


class TriggerCreate(BaseModel):
    name: str
    agent_id: str
    kind: TriggerKindName
    prompt: str
    # 由 kind 校验，格式见 TriggerKindOut.description
    config: dict[str, Any]
    # 触发前的前置检查（见 triggers.precheck）；不给则每次都交给 agent
    precheck: Precheck | None = None


class TriggerPatch(BaseModel):
    """只修改给出的字段。precheck 给出 null 表示去掉前置检查，不出现表示不变。"""

    prompt: str | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    precheck: Precheck | None = None


class _TriggerOutBase(BaseModel):
    """各触发方式共有的字段；kind、config 与 status（这种方式自己给出的运行状态，见 TriggerKind.status，不含凭证）
    由各方式的子类给出类型。"""

    id: int
    name: str
    agent_id: str
    prompt: str
    enabled: bool
    precheck: Precheck | None
    last_checked_at: datetime | None
    last_fired_at: datetime | None
    last_session_id: str | None
    # 被自动停用的原因；最近一次前置检查失败的原因
    disabled_reason: str | None
    precheck_error: str | None
    # 归档的时间：不为空表示它不会再触发了（一次性触发器跑完），只能查看与删除
    finished_at: datetime | None


class ScheduleTriggerOut(_TriggerOutBase):
    kind: Literal["schedule"]
    config: ScheduleConfig
    status: ScheduleStatus


class OnceTriggerOut(_TriggerOutBase):
    kind: Literal["once"]
    config: OnceConfig
    status: OnceStatus


class WebhookTriggerOut(_TriggerOutBase):
    kind: Literal["webhook"]
    config: WebhookConfig
    status: WebhookStatus


TriggerOut = Annotated[ScheduleTriggerOut | OnceTriggerOut | WebhookTriggerOut, Field(discriminator="kind")]
_TRIGGER_OUT: TypeAdapter[TriggerOut] = TypeAdapter(TriggerOut)


class FiredOut(BaseModel):
    # 前置检查判定这次不触发时为 null
    session_id: str | None


def _out(c: C, t: Trigger) -> TriggerOut:
    return _TRIGGER_OUT.validate_python(
        dict(
            id=t.id,
            name=t.name,
            agent_id=t.agent_id,
            kind=t.kind,
            prompt=t.prompt,
            config=t.config,
            status=c.triggers.kind(t.kind).status(t).model_dump(),
            enabled=t.enabled,
            precheck=None if t.precheck is None else Precheck.model_validate(t.precheck),
            last_checked_at=t.last_checked_at,
            last_fired_at=t.last_fired_at,
            last_session_id=t.last_session_id,
            disabled_reason=t.disabled_reason,
            precheck_error=t.precheck_error,
            finished_at=t.finished_at,
        )
    )


@router.get("/trigger-kinds")
async def trigger_kinds(c: C) -> list[TriggerKindOut]:
    return [TriggerKindOut(name=k.name, description=k.description) for k in c.triggers.kinds.values()]


@router.get("/triggers")
async def list_triggers(
    c: C, finished: bool = Query(False, description="列已归档的（跑完的一次性触发器）而不是还会触发的")
) -> list[TriggerOut]:
    return [_out(c, t) for t in await c.triggers.list(finished=finished)]


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
    """立即触发一次（不带事件内容）。有前置检查时同样先跑检查，可以用来测试整条链路。

    不消耗一次性触发器预定的那一次；已归档的触发器不能再运行。
    """
    return FiredOut(session_id=await c.triggers.run(trigger_id))
