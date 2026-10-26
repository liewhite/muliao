from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from muliao.agents.api import (
    MAX_PACKAGE_BYTES,
    TOOLS,
    USER,
    AgentInfo,
    AgentPatch,
    AgentSpec,
    Role,
    RoleSpec,
    SkillImport,
    SkillMeta,
    Thinking,
)
from muliao.api.deps import C, check_owner, read_limited

router = APIRouter()


class AgentCreate(BaseModel):
    name: str
    description: str
    # 缺省（null）时使用服务端配置的默认值
    model: str | None = None
    # 角色，不含每个 agent 隐含的 base
    roles: list[str] = []
    allow: list[str] = []
    deny: list[str] = []
    # 思考档位："default" 不传，按模型与服务端的默认
    thinking: Thinking = "default"
    # 分组名；null 或空串为不分组
    group: str | None = None


class AgentUpdate(BaseModel):
    """整体替换：所有字段必填，不会用默认值覆盖已有配置。name 可改（只用于展示）。"""

    name: str
    description: str
    model: str
    roles: list[str]
    allow: list[str]
    deny: list[str]
    thinking: Thinking
    # 分组名；null 或空串为不分组
    group: str | None


class AgentOut(BaseModel):
    id: str
    name: str
    description: str
    model: str
    roles: list[str]
    allow: list[str]
    deny: list[str]
    thinking: Thinking
    created_at: datetime
    # 系统默认 agent：Web 新对话默认选中它，最多一个
    is_default: bool
    # 分组名，null 为不分组
    group: str | None
    # 归档时间；非空时不能对话、委派、指派，它的触发器不触发
    archived_at: datetime | None

    @classmethod
    def of(cls, a: AgentInfo) -> "AgentOut":
        return cls(
            id=a.id,
            name=a.name,
            description=a.description,
            model=a.model,
            roles=list(a.roles),
            allow=list(a.allow),
            deny=list(a.deny),
            thinking=a.thinking,
            created_at=a.created_at,
            is_default=a.is_default,
            group=a.group,
            archived_at=a.archived_at,
        )


class AgentDefaultsOut(BaseModel):
    model: str


@router.get("/agent-defaults")
async def agent_defaults(c: C) -> AgentDefaultsOut:
    """新建 agent 时表单预填的默认值（与创建时套用的是同一份 AgentService.defaults）。"""
    return AgentDefaultsOut(model=c.agents.defaults.model)


class ToolOut(BaseModel):
    """一个内置工具（agents.permissions.TOOLS），在规则里写作 `tool:<name>`。"""

    name: str
    access: Literal["read", "write"]
    # 管理类工具：base 角色没有
    admin: bool

    @classmethod
    def of(cls, name: str) -> "ToolOut":
        t = TOOLS[name]
        return cls(name=name, access=t.access.value, admin=t.admin)


@router.get("/tools")
async def list_tools() -> list[ToolOut]:
    """全部已登记的内置工具，按登记顺序（含 web，即使没配置 [web]）。"""
    return [ToolOut.of(n) for n in TOOLS]


class RulesIn(BaseModel):
    roles: list[str]
    allow: list[str]
    deny: list[str]


class ResolvedOut(BaseModel):
    """按规则最终能用的内置工具。"""

    tools: list[ToolOut]

    @classmethod
    def of(cls, tools: list[str]) -> "ResolvedOut":
        return cls(tools=[ToolOut.of(t) for t in tools])


@router.post("/tools/resolve")
async def resolve_tools(c: C, body: RulesIn) -> ResolvedOut:
    """agent 配置（还没保存的也行）最终能用的内置工具：base + 角色 + allow / deny，与运行时同一套计算。"""
    return ResolvedOut.of(await c.agents.preview(body.roles, body.allow, body.deny))


@router.get("/agents/{agent_id}/items")
async def usable_items(c: C, agent_id: str) -> list[str]:
    """agent 能用的 skill、MCP server、secret（`kind:name`）：自己的与规则放行的。"""
    await c.agents.get_active(agent_id)
    return await c.agents.usable_items(agent_id)


class RoleOut(BaseModel):
    name: str
    description: str
    # 内置角色（base、admin）代码里固定的规则，不可改
    fixed_allow: list[str]
    # 可编辑的规则
    allow: list[str]
    deny: list[str]
    # 内置角色不能删；base 每个 agent 隐含
    builtin: bool
    # 只按这个角色的规则（固定的与可编辑的）能用的内置工具
    resolved: ResolvedOut

    @classmethod
    def of(cls, r: Role, resolved: list[str]) -> "RoleOut":
        return cls(
            name=r.name,
            description=r.description,
            fixed_allow=list(r.fixed.allow),
            allow=list(r.rules.allow),
            deny=list(r.rules.deny),
            builtin=r.builtin,
            resolved=ResolvedOut.of(resolved),
        )


class RoleIn(BaseModel):
    description: str
    allow: list[str]
    deny: list[str]


@router.get("/roles")
async def list_roles(c: C) -> list[RoleOut]:
    """全部角色：内置在前，自建的按名称。"""
    return [RoleOut.of(r, c.agents.resolve(r.effective)) for r in await c.roles.list()]


@router.post("/roles/{name}", status_code=201)
async def create_role(c: C, name: str, body: RoleIn) -> RoleOut:
    role = await c.roles.create(RoleSpec(name, body.description, body.allow, body.deny))
    return RoleOut.of(role, c.agents.resolve(role.effective))


@router.put("/roles/{name}")
async def update_role(c: C, name: str, body: RoleIn) -> RoleOut:
    """整体替换角色的描述与可编辑的规则（内置角色也可以，固定的部分不变）；从带它的 agent 的下一次运行起生效。"""
    role = await c.roles.update(RoleSpec(name, body.description, body.allow, body.deny))
    return RoleOut.of(role, c.agents.resolve(role.effective))


@router.delete("/roles/{name}", status_code=204)
async def delete_role(c: C, name: str) -> None:
    """有 agent 引用时 409。"""
    await c.roles.delete(name)


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
    return [AgentOut.of(a) for a in await c.agents.list()]


@router.post("/agents", status_code=201)
async def create_agent(c: C, body: AgentCreate) -> AgentOut:
    spec = AgentSpec(
        name=body.name,
        description=body.description,
        model=body.model,
        roles=body.roles,
        allow=body.allow,
        deny=body.deny,
        thinking=body.thinking,
        group=body.group,
    )
    return AgentOut.of(await c.agents.create(spec, author=USER))


@router.get("/agents/{agent_id}")
async def get_agent(c: C, agent_id: str) -> AgentOut:
    # 正在删除的按不存在处理
    return AgentOut.of(await c.agents.get_active(agent_id))


@router.put("/agents/{agent_id}")
async def update_agent(c: C, agent_id: str, body: AgentUpdate) -> AgentOut:
    # 整体替换就是所有字段都给出的修改
    patch = AgentPatch(
        name=body.name,
        description=body.description,
        model=body.model,
        roles=body.roles,
        allow=body.allow,
        deny=body.deny,
        thinking=body.thinking,
        # null 与空串都是不分组（AgentPatch 里 None 是「不变」）
        group=body.group or "",
    )
    return AgentOut.of(await c.agents.update(agent_id, patch))


@router.put("/agents/{agent_id}/default")
async def set_default_agent(c: C, agent_id: str) -> AgentOut:
    """设为系统默认 agent（取代原来的默认）。"""
    return AgentOut.of(await c.agents.set_default(agent_id, True))


@router.delete("/agents/{agent_id}/default")
async def unset_default_agent(c: C, agent_id: str) -> AgentOut:
    """取消它的系统默认身份；它不是默认时不做任何事。"""
    return AgentOut.of(await c.agents.set_default(agent_id, False))


@router.put("/agents/{agent_id}/archive")
async def archive_agent(c: C, agent_id: str) -> AgentOut:
    """归档：不能再对话、委派、指派，它的触发器不触发；它忙或是默认 agent 时 409。"""
    return AgentOut.of(await c.agent_lifecycle.set_archived(agent_id, True))


@router.delete("/agents/{agent_id}/archive")
async def unarchive_agent(c: C, agent_id: str) -> AgentOut:
    """取消归档；没归档时不做任何事。"""
    return AgentOut.of(await c.agent_lifecycle.set_archived(agent_id, False))


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(c: C, agent_id: str) -> None:
    await c.agent_lifecycle.delete(agent_id, author=USER)


class MemoryOut(BaseModel):
    content: str
    max_chars: int


class MemoryIn(BaseModel):
    # 为空即全局记忆
    owner: str | None
    content: str


@router.get("/memory")
async def get_memory(c: C, owner: str | None = None) -> MemoryOut:
    """owner 为空是全局记忆（所有 agent 共享），否则是该 agent 的记忆。"""
    await check_owner(c, owner)
    return MemoryOut(content=await c.memory.read(owner), max_chars=c.memory.max_chars)


@router.put("/memory")
async def put_memory(c: C, body: MemoryIn) -> MemoryOut:
    return MemoryOut(
        content=await c.memory.write(body.owner, body.content, USER), max_chars=c.memory.max_chars
    )


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
    """owner 名下的 skill（owner 为空是归平台的）。"""
    await check_owner(c, owner)
    return [SkillOut.of(s) for s in await c.skills.list(owner)]


@router.get("/skill")
async def get_skill(c: C, name: str) -> SkillDetail:
    skill = await c.skills.read(name)
    meta = skill.meta
    return SkillDetail(name=meta.name, description=meta.description, owner=meta.owner, body=skill.body)


@router.put("/skill")
async def put_skill(c: C, body: SkillIn) -> SkillOut:
    """新建的归 owner；已有的整体改写，owner 不变。"""
    meta = await c.skills.write(body.name, body.description, body.body, actor=USER, owner=body.owner)
    return SkillOut.of(meta)


@router.delete("/skill", status_code=204)
async def delete_skill(c: C, name: str) -> None:
    await c.skills.delete(name, USER)


SKILL_PACKAGE_TYPE = "application/zip"


@router.get(
    "/skill/package",
    response_class=Response,
    responses={200: {"content": {SKILL_PACKAGE_TYPE: {}}, "description": "<name>.skill"}},
)
async def export_skill(c: C, name: str) -> Response:
    """下载标准 .skill 文件（zip：`<name>/SKILL.md` 与附件）。"""
    package = await c.skills.export(name)
    return Response(
        package,
        media_type=SKILL_PACKAGE_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{name}.skill"'},
    )


class SkippedOut(BaseModel):
    # 包里的路径（相对 skill 目录；macOS 元数据是包里的完整路径）
    path: str
    reason: str


class SkillImportOut(BaseModel):
    skill: SkillOut
    # 没有导入的文件（二进制、不在后缀白名单里、超过单文件上限、不是 UTF-8 文本……）
    skipped: list[SkippedOut]

    @classmethod
    def of(cls, result: SkillImport) -> "SkillImportOut":
        return cls(
            skill=SkillOut.of(result.meta),
            skipped=[SkippedOut(path=s.path, reason=s.reason) for s in result.skipped],
        )


@router.post("/skill/package")
async def import_skill(
    c: C, request: Request, owner: str | None = None, replace: bool = False
) -> SkillImportOut:
    """请求体是一个标准 .skill 文件，导入为 skill（新建的归 owner，缺省为平台），一次提交、作者 user。同名 skill 已存在时 409，
    replace=true 覆盖。包的结构不对（路径越界、符号链接、顶层目录不唯一、名字与目录不一致……）422，超限 413。"""
    package = await read_limited(request, MAX_PACKAGE_BYTES)
    return SkillImportOut.of(await c.skills.import_package(package, actor=USER, owner=owner, replace=replace))
