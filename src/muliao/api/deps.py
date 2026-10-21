from typing import Annotated

from fastapi import Depends, Request

from muliao.container import Container
from muliao.errors import TooLarge


def _container(request: Request) -> Container:
    return request.app.state.container


C = Annotated[Container, Depends(_container)]


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
