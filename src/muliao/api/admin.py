"""MCP、vault、推送订阅的管理接口。"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.api.deps import C
from muliao.models import Notification
from muliao.resources import USER
from muliao.runtime.mcp import McpServer, McpSpec, McpTransport
from muliao.vault import SecretInfo

router = APIRouter()


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
            owner=m.owner,
            name=m.name,
            transport=m.transport,
            command=m.config.command,
            args=m.config.args,
            url=m.config.url,
            env=m.config.env,
            headers=m.config.headers,
            enabled=m.enabled,
            tools=tools or [],
        )


class EnabledIn(BaseModel):
    enabled: bool


@router.get("/mcp")
async def list_mcp(c: C) -> list[McpOut]:
    # env/headers 中的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回
    return [McpOut.of(m) for m in c.mcp.list_all()]


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
    if body.owner is not None:
        await c.agents.get(body.owner)
    row, tools = await c.mcp.add(body.owner, spec, probe=body.probe, author=USER)
    return McpOut.of(row, tools)


@router.post("/mcp/tools")
async def probe_mcp(c: C, name: str, owner: str | None = None) -> list[str]:
    return await c.mcp.probe(c.mcp.get(owner, name))


@router.put("/mcp/enabled")
async def set_mcp_enabled(c: C, name: str, body: EnabledIn, owner: str | None = None) -> McpOut:
    return McpOut.of(await c.mcp.set_enabled(owner, name, body.enabled, USER))


@router.delete("/mcp", status_code=204)
async def delete_mcp(c: C, name: str, owner: str | None = None) -> None:
    await c.mcp.remove(owner, name, USER)


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
