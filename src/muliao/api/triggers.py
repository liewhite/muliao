"""触发器的通用管理接口；某种触发方式专有的操作在它自己的模块里（如 api/webhook.py）。"""

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field, TypeAdapter

from muliao.agents.api import AgentNames
from muliao.api.deps import C
from muliao.platform.values import KEEP
from muliao.routing.api import ADDRESS_HELP
from muliao.triggers.api import (
    Fired,
    OnceConfig,
    OnceStatus,
    Precheck,
    ScheduleConfig,
    ScheduleStatus,
    StreamConfig,
    StreamStatus,
    TriggerFault,
    TriggerKind,
    TriggerKindName,
    TriggerView,
    WebhookConfig,
    WebhookStatus,
    route_address,
)
from muliao.triggers.tables import Trigger

router = APIRouter()


class TriggerKindOut(BaseModel):
    name: TriggerKindName
    description: str

    @classmethod
    def of(cls, k: TriggerKind) -> "TriggerKindOut":
        return cls(name=k.name, description=k.description)


class _TriggerCreateBase(BaseModel):
    """新建触发器的请求：kind 与 config 由各方式的子类给出类型（有默认值的配置项可以不填）。"""

    name: str
    agent_id: str
    prompt: str
    # 触发前的前置检查（见 triggers.precheck）；不给则每次都交给 agent
    precheck: Precheck | None = None
    # 事件的默认投递目标（文字地址，RouteHelpOut.address_help）；不给是新开这个 agent 的会话
    route: str | None = None


class ScheduleTriggerCreate(_TriggerCreateBase):
    kind: Literal["schedule"]
    config: ScheduleConfig


class OnceTriggerCreate(_TriggerCreateBase):
    kind: Literal["once"]
    config: OnceConfig


class WebhookTriggerCreate(_TriggerCreateBase):
    kind: Literal["webhook"]
    config: WebhookConfig


class StreamTriggerCreate(_TriggerCreateBase):
    kind: Literal["stream"]
    config: StreamConfig


TriggerCreate = Annotated[
    ScheduleTriggerCreate | OnceTriggerCreate | WebhookTriggerCreate | StreamTriggerCreate,
    Field(discriminator="kind"),
]


class RouteHelpOut(BaseModel):
    # 投递目标文字地址的格式说明（与给 agent 的工具说明同一份）
    address_help: str


class TriggerPatch(BaseModel):
    """只修改给出的字段。precheck 给出 null 表示去掉前置检查，不出现表示不变；route 给 `new` 回到默认。"""

    prompt: str | None = None
    # 修改请求不带 kind：按已存的 kind 校验，格式同新建时这种方式的 config
    config: dict[str, Any] | None = None
    enabled: bool | None = None
    precheck: Precheck | None = None
    route: str | None = None


class _TriggerOutBase(BaseModel):
    """各触发方式共有的字段；kind、config 与 status（这种方式自己给出的运行状态，见 TriggerKind.status，不含凭证）
    由各方式的子类给出类型。"""

    id: int
    name: str
    agent_id: str
    prompt: str
    enabled: bool
    precheck: Precheck | None
    # 事件的默认投递目标（文字地址）
    route: str
    last_checked_at: datetime | None
    last_fired_at: datetime | None
    last_session_id: str | None
    # 被自动停用的原因（重新启用时清除）
    disabled_reason: str | None
    # 脚本出错：前置检查失败、事件内容里写错而被丢掉的 `@route`（每个地址一条）、流式命令反复退出（triggers.faults）
    faults: list[TriggerFault]
    # 结束的时间：不为空表示它不会再触发了（一次性触发器跑完），只能查看与删除
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


class StreamTriggerOut(_TriggerOutBase):
    kind: Literal["stream"]
    config: StreamConfig
    status: StreamStatus


TriggerOut = Annotated[
    ScheduleTriggerOut | OnceTriggerOut | WebhookTriggerOut | StreamTriggerOut, Field(discriminator="kind")
]
_TRIGGER_OUT: TypeAdapter[TriggerOut] = TypeAdapter(TriggerOut)


class FiredOut(BaseModel):
    # 触发一次的结果（手动触发与 webhook 共用）：是否投出了信（前置检查判定这次不触发时为 false），代表这次触发的会话
    # （Fired.session：开出或续写的第一个已知会话，和触发器的 last_session_id 是同一个；只投进了 issue、或要续写的会话
    # 还没建出来时为 null）
    fired: bool
    session_id: str | None

    @classmethod
    def of(cls, fired: Fired | None) -> "FiredOut":
        return cls(fired=fired is not None, session_id=None if fired is None else fired.session)


async def _out(c: C, t: Trigger) -> TriggerOut:
    return _out_of(c.triggers.view(t), await c.agents.names())


def _out_of(view: TriggerView, names: AgentNames) -> TriggerOut:
    t = view.trigger
    return _TRIGGER_OUT.validate_python(
        dict(
            id=t.id,
            name=t.name,
            agent_id=t.agent_id,
            kind=t.kind,
            prompt=t.prompt,
            config=t.config,
            status=view.status,
            enabled=t.enabled,
            precheck=view.precheck,
            route=route_address(t, names),
            last_checked_at=t.last_checked_at,
            last_fired_at=t.last_fired_at,
            last_session_id=t.last_session_id,
            disabled_reason=t.disabled_reason,
            faults=view.faults,
            finished_at=t.finished_at,
        )
    )


@router.get("/trigger-kinds")
async def trigger_kinds(c: C) -> list[TriggerKindOut]:
    return [TriggerKindOut.of(k) for k in c.triggers.kinds.values()]


@router.get("/trigger-routes/help")
async def route_help() -> RouteHelpOut:
    return RouteHelpOut(address_help=ADDRESS_HELP)


@router.get("/triggers")
async def list_triggers(
    c: C, finished: bool = Query(False, description="列已结束的（跑完的一次性触发器）而不是还会触发的")
) -> list[TriggerOut]:
    names = await c.agents.names()
    return [_out_of(c.triggers.view(t), names) for t in await c.triggers.list(finished=finished)]


@router.post("/triggers", status_code=201)
async def create_trigger(c: C, body: TriggerCreate) -> TriggerOut:
    trigger = await c.triggers.create(
        name=body.name,
        agent_id=body.agent_id,
        kind=body.kind,
        prompt=body.prompt,
        # 原样交给触发方式规范化（没填的默认值由它补上）
        config=body.config.model_dump(mode="json", exclude_unset=True),
        precheck=body.precheck,
        route=body.route,
    )
    return await _out(c, trigger)


@router.patch("/triggers/{trigger_id}")
async def update_trigger(c: C, trigger_id: int, body: TriggerPatch) -> TriggerOut:
    trigger = await c.triggers.update(
        trigger_id,
        prompt=body.prompt,
        config=body.config,
        enabled=body.enabled,
        precheck=body.precheck if "precheck" in body.model_fields_set else KEEP,
        route=body.route,
    )
    return await _out(c, trigger)


@router.delete("/triggers/{trigger_id}", status_code=204)
async def delete_trigger(c: C, trigger_id: int) -> None:
    await c.triggers.delete(trigger_id)


@router.post("/triggers/{trigger_id}/run", status_code=202)
async def run_trigger(c: C, trigger_id: int) -> FiredOut:
    """立即触发一次（不带事件内容）。有前置检查时同样先跑检查，可以用来测试整条链路。

    不消耗一次性触发器预定的那一次；已结束的触发器不能再运行。
    """
    return FiredOut.of(await c.triggers.run(trigger_id))
