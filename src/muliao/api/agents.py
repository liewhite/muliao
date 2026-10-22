from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel

from muliao.agents.api import USER, AgentInfo, AgentPatch, AgentSpec, SkillMeta, ToolGroupInfo
from muliao.api.deps import C, check_owner
from muliao.api.resources import CorruptOut
from muliao.reflection.tables import Reflection

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
    # 自上次反思以来的步数（模型回复数），由消息推导
    steps_since_reflection: int
    created_at: datetime

    @classmethod
    def of(cls, a: AgentInfo, instructions: str, steps_since_reflection: int) -> "AgentOut":
        return cls(
            id=a.id,
            name=a.name,
            description=a.description,
            model=a.model,
            instructions=instructions,
            reflect_every=a.reflect_every,
            tool_groups=list(a.tool_groups),
            steps_since_reflection=steps_since_reflection,
            created_at=a.created_at,
        )


async def _outs(c: C, agents: list[AgentInfo]) -> list[AgentOut]:
    """AgentOut 的唯一构造入口：一次取齐这些 agent 的反思进度。"""
    steps = await c.reflection.steps_since(agents)
    return [AgentOut.of(a, c.prompts.current(a.id), steps[a.id]) for a in agents]


async def _out(c: C, agent: AgentInfo) -> AgentOut:
    [out] = await _outs(c, [agent])
    return out


class ToolGroupOut(BaseModel):
    """一个可选的工具组（tool_catalog）。"""

    name: str
    label: str

    @classmethod
    def of(cls, g: ToolGroupInfo) -> "ToolGroupOut":
        return cls(name=g.name, label=g.label)


class AgentDefaultsOut(BaseModel):
    model: str
    reflect_every: int
    # 当前配置下可选的工具组，按目录顺序
    groups: list[ToolGroupOut]
    # 新建 agent 默认开启的组：当前配置下的全部组
    default_tool_groups: list[str]


@router.get("/agent-defaults")
async def agent_defaults(c: C) -> AgentDefaultsOut:
    """新建 agent 时表单预填的默认值（与创建时套用的是同一份 AgentService.defaults）。"""
    d = c.agents.defaults
    return AgentDefaultsOut(
        model=d.model,
        reflect_every=d.reflect_every,
        groups=[ToolGroupOut.of(g) for g in d.groups],
        default_tool_groups=d.group_names,
    )


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
    return await _outs(c, await c.agents.list())


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
    return MemoryOut(content=await c.memory.write(agent_id, body.content, USER), max_chars=c.memory.max_chars)


class ReflectionOut(BaseModel):
    id: int
    from_message_id: int
    to_message_id: int
    output: str
    created_at: datetime

    @classmethod
    def of(cls, r: Reflection) -> "ReflectionOut":
        return cls(
            id=r.id,
            from_message_id=r.from_message_id,
            to_message_id=r.to_message_id,
            output=r.output,
            created_at=r.created_at,
        )


@router.get("/agents/{agent_id}/reflections")
async def list_reflections(c: C, agent_id: str, limit: int = Query(50, ge=1, le=200)) -> list[ReflectionOut]:
    await c.agents.get(agent_id)
    return [ReflectionOut.of(r) for r in await c.reflection.list(agent_id, limit)]


class SkillOut(BaseModel):
    name: str
    description: str
    owner: str | None

    @classmethod
    def of(cls, m: SkillMeta) -> "SkillOut":
        return cls(name=m.name, description=m.description, owner=m.owner)


class SkillDetail(SkillOut):
    body: str


class SkillIn(BaseModel):
    owner: str | None
    name: str
    description: str
    body: str


@router.get("/skills")
async def list_skills(c: C, owner: str | None = None) -> list[SkillOut]:
    """owner 为空列出公共 skill，否则列出该 agent 的私有 skill。"""
    await check_owner(c, owner)
    return [SkillOut.of(s) for s in c.skills.list(owner)]


@router.get("/skills/corrupt")
async def list_corrupt_skills(c: C, owner: str | None = None) -> list[CorruptOut]:
    """owner 名下（缺省为公共）读不出来的 skill，GET /skills 不列它们。"""
    await check_owner(c, owner)
    return [CorruptOut.of(x) for x in c.skills.corrupt_in(owner)]


@router.get("/skill")
async def get_skill(c: C, name: str, owner: str | None = None) -> SkillDetail:
    await check_owner(c, owner)
    skill = c.skills.read(owner, name)
    return SkillDetail(name=skill.meta.name, description=skill.meta.description, owner=owner, body=skill.body)


@router.put("/skill")
async def put_skill(c: C, body: SkillIn) -> SkillOut:
    meta = await c.skills.write(body.owner, body.name, body.description, body.body, USER)
    return SkillOut.of(meta)


@router.delete("/skill", status_code=204)
async def delete_skill(c: C, name: str, owner: str | None = None) -> None:
    await c.skills.delete(owner, name, USER)
