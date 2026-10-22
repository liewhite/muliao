"""webhook 触发方式专有的接口：外部系统的事件入口（凭 token 鉴权，不走登录），以及用户生成 token。"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from muliao.api.deps import C, read_limited
from muliao.api.triggers import FiredOut
from muliao.errors import InvalidInput, Unauthorized
from muliao.triggers.api import EVENT_MAX_BYTES, PrecheckFailed

# 不需要登录：外部系统调用
public_router = APIRouter()
# 需要登录：用户在界面上操作
router = APIRouter()


class TokenOut(BaseModel):
    # 明文只返回这一次
    token: str


@public_router.post("/api/hooks/{name}", status_code=202)
async def hook(c: C, name: str, request: Request, session_id: str | None = None) -> FiredOut:
    """外部事件入口：Authorization: Bearer <token>。不带 session_id 时新建会话；带上之前返回的则在该会话里继续。"""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("missing bearer token")
    # 先鉴权再读请求体：未鉴权的调用方不能让进程读入任何内容
    trigger_id = await c.webhooks.authenticate(name, token)
    payload = await read_limited(request, EVENT_MAX_BYTES)
    try:
        text = payload.decode()
    except UnicodeDecodeError as e:
        raise InvalidInput("event payload must be UTF-8 text") from e
    try:
        delivered = await c.webhooks.deliver(trigger_id, text, session_id)
    except PrecheckFailed as e:
        # 原因（可能含脚本的 stderr）只记在触发器上给用户看，不交给外部调用方
        raise HTTPException(status_code=502, detail="trigger precheck failed") from e
    return FiredOut(session_id=delivered)


@router.post("/triggers/{trigger_id}/webhook/token")
async def generate_token(c: C, trigger_id: int) -> TokenOut:
    """生成（或重新生成）webhook 的 token；只能由用户在这里生成，旧 token 立即失效。"""
    return TokenOut(token=await c.webhooks.generate_token(trigger_id))
