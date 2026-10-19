"""cron、事件触发器、MCP、vault、推送订阅的管理接口。"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.delivery.base import Notification
from muliao.models import CronJob, EventTrigger, McpServer, McpTransport
from muliao.runtime.mcp import McpSpec
from muliao.triggers.events import hook_url
from muliao.vault import SecretInfo

router = APIRouter()


# ---- cron ----


class CronIn(BaseModel):
    schedule: str
    prompt: str
    enabled: bool = True


class CronCreate(BaseModel):
    name: str
    agent_id: str
    schedule: str
    prompt: str


class CronOut(BaseModel):
    id: int
    name: str
    agent_id: str
    schedule: str
    prompt: str
    enabled: bool
    next_run_at: datetime
    last_run_at: datetime | None
    last_session_id: str | None
    last_error: str | None

    @classmethod
    def of(cls, j: CronJob) -> "CronOut":
        return cls(
            id=j.id,
            name=j.name,
            agent_id=j.agent_id,
            schedule=j.schedule,
            prompt=j.prompt,
            enabled=j.enabled,
            next_run_at=j.next_run_at,
            last_run_at=j.last_run_at,
            last_session_id=j.last_session_id,
            last_error=j.last_error,
        )


@router.get("/cron")
async def list_cron(c: C) -> list[CronOut]:
    return [CronOut.of(j) for j in await c.cron.list()]


@router.post("/cron", status_code=201)
async def create_cron(c: C, body: CronCreate) -> CronOut:
    return CronOut.of(
        await c.cron.create(
            name=body.name, agent_id=body.agent_id, schedule=body.schedule, prompt=body.prompt
        )
    )


@router.put("/cron/{job_id}")
async def update_cron(c: C, job_id: int, body: CronIn) -> CronOut:
    return CronOut.of(
        await c.cron.update(job_id, schedule=body.schedule, prompt=body.prompt, enabled=body.enabled)
    )


@router.delete("/cron/{job_id}", status_code=204)
async def delete_cron(c: C, job_id: int) -> None:
    await c.cron.delete(job_id)


# ---- 事件触发器 ----


class EventTriggerIn(BaseModel):
    prompt: str
    enabled: bool = True


class EventTriggerCreate(BaseModel):
    name: str
    agent_id: str
    prompt: str


class EventTriggerOut(BaseModel):
    id: int
    name: str
    agent_id: str
    prompt: str
    enabled: bool
    # 外部系统调用的完整地址
    url: str
    last_fired_at: datetime | None
    last_session_id: str | None

    @classmethod
    def of(cls, t: EventTrigger, public_url: str) -> "EventTriggerOut":
        return cls(
            id=t.id,
            name=t.name,
            agent_id=t.agent_id,
            prompt=t.prompt,
            enabled=t.enabled,
            url=hook_url(public_url, t.name),
            last_fired_at=t.last_fired_at,
            last_session_id=t.last_session_id,
        )


class EventTriggerCreated(BaseModel):
    trigger: EventTriggerOut
    # 明文只返回这一次
    token: str


class TokenOut(BaseModel):
    token: str


@router.get("/event-triggers")
async def list_event_triggers(c: C) -> list[EventTriggerOut]:
    return [EventTriggerOut.of(t, c.settings.server.public_url) for t in await c.events.list()]


@router.post("/event-triggers", status_code=201)
async def create_event_trigger(c: C, body: EventTriggerCreate) -> EventTriggerCreated:
    trigger, token = await c.events.create(name=body.name, agent_id=body.agent_id, prompt=body.prompt)
    return EventTriggerCreated(trigger=EventTriggerOut.of(trigger, c.settings.server.public_url), token=token)


@router.put("/event-triggers/{trigger_id}")
async def update_event_trigger(c: C, trigger_id: int, body: EventTriggerIn) -> EventTriggerOut:
    trigger = await c.events.update(trigger_id, prompt=body.prompt, enabled=body.enabled)
    return EventTriggerOut.of(trigger, c.settings.server.public_url)


@router.post("/event-triggers/{trigger_id}/token")
async def rotate_event_trigger_token(c: C, trigger_id: int) -> TokenOut:
    return TokenOut(token=await c.events.rotate_token(trigger_id))


@router.delete("/event-triggers/{trigger_id}", status_code=204)
async def delete_event_trigger(c: C, trigger_id: int) -> None:
    await c.events.delete(trigger_id)


# ---- MCP ----


class McpIn(BaseModel):
    owner: str | None
    name: str
    transport: McpTransport
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    probe: bool = True


class McpOut(BaseModel):
    id: int
    owner: str | None
    name: str
    transport: McpTransport
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    enabled: bool
    tools: list[str] = []

    @classmethod
    def of(cls, m: McpServer, tools: list[str] | None = None) -> "McpOut":
        return cls(
            id=m.id,
            owner=m.owner,
            name=m.name,
            transport=m.transport,
            command=m.command,
            args=m.args,
            url=m.url,
            env=m.env,
            headers=m.headers,
            enabled=m.enabled,
            tools=tools or [],
        )


class EnabledIn(BaseModel):
    enabled: bool


@router.get("/mcp")
async def list_mcp(c: C) -> list[McpOut]:
    # env/headers 中的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回
    return [McpOut.of(m) for m in await c.mcp.list(None, all_owners=True)]


@router.post("/mcp", status_code=201)
async def add_mcp(c: C, body: McpIn) -> McpOut:
    spec = McpSpec(
        name=body.name,
        transport=body.transport,
        command=body.command,
        args=tuple(body.args),
        url=body.url,
        env=tuple(body.env.items()),
        headers=tuple(body.headers.items()),
    )
    row, tools = await c.mcp.add(body.owner, spec, probe=body.probe)
    return McpOut.of(row, tools)


@router.post("/mcp/tools")
async def probe_mcp(c: C, name: str, owner: str | None = None) -> list[str]:
    return await c.mcp.probe(await c.mcp.get(owner, name))


@router.put("/mcp/enabled")
async def set_mcp_enabled(c: C, name: str, body: EnabledIn, owner: str | None = None) -> McpOut:
    return McpOut.of(await c.mcp.set_enabled(owner, name, body.enabled))


@router.delete("/mcp", status_code=204)
async def delete_mcp(c: C, name: str, owner: str | None = None) -> None:
    await c.mcp.remove(owner, name)


# ---- vault ----


class SecretIn(BaseModel):
    value: str
    description: str = ""


class SecretOut(BaseModel):
    # None 为公共
    owner: str | None
    key: str
    description: str
    updated_at: datetime

    @classmethod
    def of(cls, s: SecretInfo) -> "SecretOut":
        return cls(owner=s.owner, key=s.key, description=s.description, updated_at=s.updated_at)


@router.get("/vault")
async def list_secrets(c: C) -> list[SecretOut]:
    return [SecretOut.of(s) for s in await c.vault.list_all()]


@router.put("/vault/{key}", status_code=204)
async def set_secret(c: C, key: str, body: SecretIn, owner: str | None = None) -> None:
    """owner 缺省为公共 secret，否则为该 agent 私有。"""
    await c.vault.set(owner, key, body.value, body.description)


@router.delete("/vault/{key}", status_code=204)
async def delete_secret(c: C, key: str, owner: str | None = None) -> None:
    await c.vault.delete(owner, key)


# ---- push ----


class PushKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionIn(BaseModel):
    endpoint: str
    keys: PushKeys


class VapidOut(BaseModel):
    public_key: str


@router.get("/push/vapid")
async def vapid_key(c: C) -> VapidOut:
    return VapidOut(public_key=c.vapid.public_key_b64)


@router.post("/push/subscriptions", status_code=204)
async def subscribe(c: C, body: PushSubscriptionIn) -> None:
    await c.push_subscriptions.add(body.endpoint, body.keys.p256dh, body.keys.auth)


@router.post("/push/test", status_code=202)
async def test_push(c: C) -> None:
    await c.notifier.notify(
        None,
        Notification(
            title="Muliao",
            body="推送通知已开启。",
            session_id=None,
        ),
    )
