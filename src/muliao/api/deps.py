from typing import Annotated, Any, get_args

from fastapi import Depends, Request

from muliao.container import Container
from muliao.errors import TooLarge
from muliao.schema import Domain, Resource

_RESOURCES = {*get_args(Domain), "tasks"}


def reads(*resources: Resource) -> dict[str, Any]:
    """GET 接口的输出读了哪些数据（归属见 docs/architecture.md 2.4 第 16 行），写在路由上：
    `@router.get(..., openapi_extra=reads("issues", "sessions"))`。经 OpenAPI 扩展 x-reads 导出，前端请求这个接口就订阅这些
    类别的 changed；实际查的表必须被声明覆盖（tests/test_reads.py）。不随服务端数据变化的（配置、说明）不带参数。"""
    unknown = set(resources) - _RESOURCES
    if unknown:
        raise ValueError(f"unknown resources {sorted(unknown)}")
    return {"x-reads": sorted(resources)}


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
