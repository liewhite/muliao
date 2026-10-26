"""MCP、vault、推送订阅的管理接口。"""

from datetime import datetime

from fastapi import APIRouter, Response
from pydantic import BaseModel

from muliao.agents.api import USER, McpServer, McpTransport, mcp_config
from muliao.api.deps import C, check_owner
from muliao.vault.api import SecretInfo

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
async def list_mcp(c: C, owner: str | None = None) -> list[McpOut]:
    """owner 视角下的 server：它私有的在前，然后是公共的；owner 缺省时只列公共 server。"""
    # env/headers 中的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回
    await check_owner(c, owner)
    return [McpOut.of(m) for m in await c.mcp.view(owner)]


@router.post("/mcp", status_code=201)
async def add_mcp(c: C, body: McpIn) -> McpOut:
    config = mcp_config(
        transport=body.transport,
        command=body.command,
        args=list(body.args),
        url=body.url,
        env=dict(body.env),
        headers=dict(body.headers),
    )
    row, tools = await c.mcp.add(body.owner, body.name, config, probe=body.probe, author=USER)
    return McpOut.of(row, tools)


@router.post("/mcp/tools")
async def probe_mcp(c: C, name: str, owner: str | None = None) -> list[str]:
    return await c.mcp.probe(await c.mcp.get(owner, name))


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
async def list_secrets(c: C, owner: str | None = None) -> list[SecretOut]:
    """owner 视角下的 secret（不含值）：它私有的在前，然后是公共的；owner 缺省时只列公共 secret。"""
    await check_owner(c, owner)
    return [SecretOut.of(s) for s in await c.vault.visible_to(owner)]


@router.put("/vault/{key}", status_code=204)
async def set_secret(c: C, key: str, body: SecretIn, owner: str | None = None) -> None:
    """owner 缺省为公共 secret，否则为该 agent 私有。"""
    await c.vault.set(owner, key, body.value, body.description, USER)


@router.delete("/vault/{key}", status_code=204)
async def delete_secret(c: C, key: str, owner: str | None = None) -> None:
    await c.vault.delete(owner, key, USER)


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


@router.post("/push/test", status_code=202, response_class=Response)
async def test_push(c: C) -> None:
    await c.notifications.send_test()
