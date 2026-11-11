"""模型（agent_runtime 的模型注册）的接口：配置里的 provider 与各 provider 的模型列表，供界面选 agent 的模型。"""

from fastapi import APIRouter

from muliao.api.deps import C

router = APIRouter()


@router.get("/providers")
async def list_providers(c: C) -> list[str]:
    """配置里的 LLM provider 名，即 agent 模型 "<provider 名>:<模型名>" 的前半部分。"""
    return c.models.provider_names()


@router.get("/providers/{name}/models")
async def list_provider_models(c: C, name: str) -> list[str]:
    """向该 provider 实时查询的模型名；provider 出错时为 502 并带上原因。"""
    return await c.models.list_models(name)
