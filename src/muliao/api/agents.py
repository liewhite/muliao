from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agents import AgentSpec
from muliao.api.deps import C
from muliao.models import Agent
from muliao.runtime.reflection import list_reflections_of

router = APIRouter()


class AgentCreate(BaseModel):
    name: str
    description: str
    # 缺省（null）时使用服务端配置的默认值
    model: str | None = None
    instructions: str = ""
    reflect_every: int | None = None


class AgentUpdate(BaseModel):
    """整体替换：所有字段必填，不会用默认值覆盖已有配置。"""

    description: str
    model: str
    instructions: str
    reflect_every: int


class AgentOut(BaseModel):
    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int
    turns_since_reflection: int
    created_at: datetime

    @classmethod
    def of(cls, a: Agent) -> "AgentOut":
        return cls(
            name=a.name,
            description=a.description,
            model=a.model,
            instructions=a.instructions,
            reflect_every=a.reflect_every,
            turns_since_reflection=a.turns_since_reflection,
            created_at=a.created_at,
        )


def _create_spec(c: C, body: AgentCreate) -> AgentSpec:
    return AgentSpec(
        description=body.description,
        model=c.settings.llm.default_model if body.model is None else body.model,
        instructions=body.instructions,
        reflect_every=c.settings.runtime.default_reflect_every
        if body.reflect_every is None
        else body.reflect_every,
    )


class AgentDefaults(BaseModel):
    model: str
    reflect_every: int


@router.get("/agent-defaults")
async def agent_defaults(c: C) -> AgentDefaults:
    """新建 agent 时表单预填的默认值；唯一来源是服务端配置。"""
    return AgentDefaults(
        model=c.settings.llm.default_model, reflect_every=c.settings.runtime.default_reflect_every
    )


@router.get("/agents")
async def list_agents(c: C) -> list[AgentOut]:
    return [AgentOut.of(a) for a in await c.agents.list()]


@router.post("/agents", status_code=201)
async def create_agent(c: C, body: AgentCreate) -> AgentOut:
    return AgentOut.of(await c.agents.create(body.name, _create_spec(c, body)))


@router.get("/agents/{name}")
async def get_agent(c: C, name: str) -> AgentOut:
    return AgentOut.of(await c.agents.get(name))


@router.put("/agents/{name}")
async def update_agent(c: C, name: str, body: AgentUpdate) -> AgentOut:
    spec = AgentSpec(
        description=body.description,
        model=body.model,
        instructions=body.instructions,
        reflect_every=body.reflect_every,
    )
    return AgentOut.of(await c.agents.update(name, spec))


@router.delete("/agents/{name}", status_code=204)
async def delete_agent(c: C, name: str) -> None:
    await c.agents.delete(name)


class MemoryOut(BaseModel):
    content: str
    max_chars: int


class MemoryIn(BaseModel):
    content: str


@router.get("/agents/{name}/memory")
async def get_memory(c: C, name: str) -> MemoryOut:
    await c.agents.get(name)
    return MemoryOut(content=c.memory.read(name), max_chars=c.memory.max_chars)


@router.put("/agents/{name}/memory")
async def put_memory(c: C, name: str, body: MemoryIn) -> MemoryOut:
    await c.agents.get(name)
    return MemoryOut(content=c.memory.write(name, body.content), max_chars=c.memory.max_chars)


class ReflectionOut(BaseModel):
    id: int
    from_message_id: int
    to_message_id: int
    output: str
    created_at: datetime


@router.get("/agents/{name}/reflections")
async def list_reflections(c: C, name: str, limit: int = 50) -> list[ReflectionOut]:
    await c.agents.get(name)
    return [
        ReflectionOut(
            id=r.id,
            from_message_id=r.from_message_id,
            to_message_id=r.to_message_id,
            output=r.output,
            created_at=r.created_at,
        )
        for r in await list_reflections_of(c.db, name, limit)
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
    meta = c.skills.write(body.owner, body.name, body.description, body.body)
    return SkillOut(name=meta.name, description=meta.description, owner=meta.owner)


@router.delete("/skill", status_code=204)
async def delete_skill(c: C, name: str, owner: str | None = None) -> None:
    await _check_owner(c, owner)
    c.skills.delete(owner, name)
