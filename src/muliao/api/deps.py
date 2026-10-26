from typing import Annotated

from fastapi import Depends, Request

from muliao.container import Container
from muliao.errors import TooLarge


def _container(request: Request) -> Container:
    return request.app.state.container


C = Annotated[Container, Depends(_container)]


async def check_owner(c: Container, owner: str | None) -> None:
    """读 owner 的私有资源之前（列出、读历史）：正在删除的 agent 按不存在处理。写入时在写入的事务里检查（agents.active）。"""
    if owner is not None:
        await c.agents.get_active(owner)


async def read_limited(request: Request, limit: int) -> bytes:
    """按字节上限读取请求体：声明的长度超限直接拒绝，未声明长度的分块读取，超限即停。"""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise TooLarge(f"request body exceeds {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise TooLarge(f"request body exceeds {limit} bytes")
    return bytes(body)
