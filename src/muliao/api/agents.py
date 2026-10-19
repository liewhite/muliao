from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agents import AgentPatch, AgentSpec
from muliao.api.deps import C
from muliao.models import Agent
from muliao.resources import USER
from muliao.runtime.reflection import list_reflections_of

router = APIRouter()


class AgentCreate(BaseModel):
    name: str
    description: str
    # 缺省（null）时使用服务端配置的默认值
    model: str | None = None
    instructions: str = ""
    reflect_every: int | None = None
    tool_groups: list[str] | None = None


class AgentUpdate(BaseModel):
    """整体替换：所有字段必填，不会用默认值覆盖已有配置。name 可改（只用于展示）。"""

    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int
    tool_groups: list[str]


class AgentOut(BaseModel):
    id: str
    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int
    tool_groups: list[str]
    turns_since_reflection: int
    created_at: datetime

    @classmethod
    def of(cls, a: Agent, instructions: str) -> "AgentOut":
        return cls(
            id=a.id,
            name=a.name,
            description=a.description,
            model=a.model,
            instructions=instructions,
            reflect_every=a.reflect_every,
            tool_groups=a.tool_groups,
            turns_since_reflection=a.turns_since_reflection,
            created_at=a.created_at,
        )


async def _out(c: C, agent: Agent) -> AgentOut:
    return AgentOut.of(agent, c.prompts.current(agent.id))


class AgentDefaultsOut(BaseModel):
    model: str
    reflect_every: int
    # 所有可选的工具组；新建 agent 默认全部开放
    tool_groups: list[str]


@router.get("/agent-defaults")
async def agent_defaults(c: C) -> AgentDefaultsOut:
    """新建 agent 时表单预填的默认值（与创建时套用的是同一份 AgentService.defaults）。"""
    d = c.agents.defaults
    return AgentDefaultsOut(model=d.model, reflect_every=d.reflect_every, tool_groups=list(d.tool_groups))


@router.get("/providers")
async def list_providers(c: C) -> list[str]:
    """配置里的 LLM provider 名，即 agent 模型 "<provider 名>:<模型名>" 的前半部分。"""
    return c.models.provider_names()


@router.get("/providers/{name}/models")
async def list_provider_models(c: C, name: str) -> list[str]:
    """向该 provider 实时查询的模型名；provider 出错时为 502 并带上原因。"""
    return await c.models.list_models(name)


@router.get("/agents")
async def list_agents(c: C) -> list[AgentOut]:
    agents = await c.agents.list()
    return [AgentOut.of(a, c.prompts.current(a.id)) for a in agents]


@router.post("/agents", status_code=201)
async def create_agent(c: C, body: AgentCreate) -> AgentOut:
    spec = AgentSpec(
        name=body.name,
        description=body.description,
        instructions=body.instructions,
        model=body.model,
        reflect_every=body.reflect_every,
        tool_groups=body.tool_groups,
    )
    return await _out(c, await c.agents.create(spec, author=USER))


@router.get("/agents/{agent_id}")
async def get_agent(c: C, agent_id: str) -> AgentOut:
    return await _out(c, await c.agents.get(agent_id))


@router.put("/agents/{agent_id}")
async def update_agent(c: C, agent_id: str, body: AgentUpdate) -> AgentOut:
    # 整体替换就是所有字段都给出的修改
    patch = AgentPatch(
        name=body.name,
        description=body.description,
        model=body.model,
        instructions=body.instructions,
        reflect_every=body.reflect_every,
        tool_groups=body.tool_groups,
    )
    return await _out(c, await c.agents.update(agent_id, patch, author=USER))


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(c: C, agent_id: str) -> None:
    await c.agent_lifecycle.delete(agent_id, author=USER)


class MemoryOut(BaseModel):
    content: str
    max_chars: int


class MemoryIn(BaseModel):
    content: str


@router.get("/agents/{agent_id}/memory")
async def get_memory(c: C, agent_id: str) -> MemoryOut:
    await c.agents.get(agent_id)
    return MemoryOut(content=c.memory.read(agent_id), max_chars=c.memory.max_chars)


@router.put("/agents/{agent_id}/memory")
async def put_memory(c: C, agent_id: str, body: MemoryIn) -> MemoryOut:
    await c.agents.get(agent_id)
    return MemoryOut(content=await c.memory.write(agent_id, body.content, USER), max_chars=c.memory.max_chars)


class ReflectionOut(BaseModel):
    id: int
    from_message_id: int
    to_message_id: int
    output: str
    created_at: datetime


@router.get("/agents/{agent_id}/reflections")
async def list_reflections(c: C, agent_id: str, limit: int = 50) -> list[ReflectionOut]:
    await c.agents.get(agent_id)
    return [
        ReflectionOut(
            id=r.id,
            from_message_id=r.from_message_id,
            to_message_id=r.to_message_id,
            output=r.output,
            created_at=r.created_at,
        )
        for r in await list_reflections_of(c.db, agent_id, limit)
    ]


class SkillOut(BaseModel):
    name: str
    description: str
    owner: str | None


class SkillDetail(SkillOut):
    body: str


class SkillIn(BaseModel):
    owner: str | None
    name: str
    description: str
    body: str


async def _check_owner(c: C, owner: str | None) -> None:
    if owner is not None:
        await c.agents.get(owner)


@router.get("/skills")
async def list_skills(c: C, owner: str | None = None) -> list[SkillOut]:
    """owner 为空列出公共 skill，否则列出该 agent 的私有 skill。"""
    await _check_owner(c, owner)
    return [SkillOut(name=s.name, description=s.description, owner=s.owner) for s in c.skills.list(owner)]


@router.get("/skill")
async def get_skill(c: C, name: str, owner: str | None = None) -> SkillDetail:
    await _check_owner(c, owner)
    skill = c.skills.read(owner, name)
    return SkillDetail(name=skill.meta.name, description=skill.meta.description, owner=owner, body=skill.body)


@router.put("/skill")
async def put_skill(c: C, body: SkillIn) -> SkillOut:
    await _check_owner(c, body.owner)
    meta = await c.skills.write(body.owner, body.name, body.description, body.body, USER)
    return SkillOut(name=meta.name, description=meta.description, owner=meta.owner)


@router.delete("/skill", status_code=204)
async def delete_skill(c: C, name: str, owner: str | None = None) -> None:
    await _check_owner(c, owner)
    await c.skills.delete(owner, name, USER)
