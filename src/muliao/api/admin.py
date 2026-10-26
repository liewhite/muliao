"""MCP、vault、推送订阅的管理接口。"""

from fastapi import APIRouter, Response
from pydantic import BaseModel

from muliao.agents.api import USER, McpServer, McpTransport, mcp_config
from muliao.api.deps import C

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
    """全部 server（含停用的），按名称；谁能用见 /items 的 roles。"""
    # env/headers 中的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回
    return [McpOut.of(m) for m in await c.mcp.usable(USER)]


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
    row, tools = await c.mcp.add(body.name, config, actor=USER, owner=body.owner, probe=body.probe)
    return McpOut.of(row, tools)


@router.post("/mcp/tools")
async def probe_mcp(c: C, name: str) -> list[str]:
    return await c.mcp.probe(await c.mcp.get(name), USER)


@router.put("/mcp/enabled")
async def set_mcp_enabled(c: C, name: str, body: EnabledIn) -> McpOut:
    return McpOut.of(await c.mcp.set_enabled(name, body.enabled, USER))


@router.delete("/mcp", status_code=204)
async def delete_mcp(c: C, name: str) -> None:
    await c.mcp.remove(name, USER)


# ---- vault ----


class SecretIn(BaseModel):
    value: str
    description: str = ""


@router.put("/vault/{key}", status_code=204)
async def set_secret(c: C, key: str, body: SecretIn, owner: str | None = None) -> None:
    """新建的归 owner（缺省为平台）；已有的改值与描述，owner 不变。列表见 /items（不含值）。"""
    await c.vault.set(key, body.value, body.description, actor=USER, owner=owner)


@router.delete("/vault/{key}", status_code=204)
async def delete_secret(c: C, key: str) -> None:
    await c.vault.delete(key, USER)


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
