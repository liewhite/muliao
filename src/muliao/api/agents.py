from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agents import AgentSpec
from muliao.api.deps import C
from muliao.evolution.prompts import USER
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
    """整体替换：所有字段必填，不会用默认值覆盖已有配置。name 可改（只用于展示）。"""

    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int


class AgentOut(BaseModel):
    id: str
    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int
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
            turns_since_reflection=a.turns_since_reflection,
            created_at=a.created_at,
        )


async def _out(c: C, agent: Agent) -> AgentOut:
    return AgentOut.of(agent, await c.prompts.current(agent.id))


def _create_spec(c: C, body: AgentCreate) -> AgentSpec:
    return AgentSpec(
        name=body.name,
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
    agents = await c.agents.list()
    instructions = await c.prompts.current_many([a.id for a in agents])
    return [AgentOut.of(a, instructions[a.id]) for a in agents]


@router.post("/agents", status_code=201)
async def create_agent(c: C, body: AgentCreate) -> AgentOut:
    return await _out(c, await c.agents.create(_create_spec(c, body), author=USER))


@router.get("/agents/{agent_id}")
async def get_agent(c: C, agent_id: str) -> AgentOut:
    return await _out(c, await c.agents.get(agent_id))


@router.put("/agents/{agent_id}")
async def update_agent(c: C, agent_id: str, body: AgentUpdate) -> AgentOut:
    spec = AgentSpec(
        name=body.name,
        description=body.description,
        model=body.model,
        instructions=body.instructions,
        reflect_every=body.reflect_every,
    )
    return await _out(c, await c.agents.update(agent_id, spec, author=USER))


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(c: C, agent_id: str) -> None:
    await c.agents.delete(agent_id)


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
    return MemoryOut(content=c.memory.write(agent_id, body.content), max_chars=c.memory.max_chars)


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
    meta = c.skills.write(body.owner, body.name, body.description, body.body)
    return SkillOut(name=meta.name, description=meta.description, owner=meta.owner)


@router.delete("/skill", status_code=204)
async def delete_skill(c: C, name: str, owner: str | None = None) -> None:
    await _check_owner(c, owner)
    c.skills.delete(owner, name)
