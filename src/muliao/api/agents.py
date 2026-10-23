from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from muliao.agents.api import (
    MAX_PACKAGE_BYTES,
    TOOL_PERMISSIONS,
    USER,
    AgentInfo,
    AgentPatch,
    AgentSpec,
    Permission,
    Resolved,
    Role,
    RoleSpec,
    SkillImport,
    SkillMeta,
    Thinking,
)
from muliao.api.deps import C, check_owner, read_limited
from muliao.api.resources import CorruptOut

router = APIRouter()


class AgentCreate(BaseModel):
    name: str
    description: str
    # 缺省（null）时使用服务端配置的默认值
    model: str | None = None
    # 缺省（null）时为 AgentDefaults.roles
    roles: list[str] | None = None
    allow: list[str] = []
    deny: list[str] = []
    # 思考档位："default" 不传，按模型与服务端的默认
    thinking: Thinking = "default"


class AgentUpdate(BaseModel):
    """整体替换：所有字段必填，不会用默认值覆盖已有配置。name 可改（只用于展示）。"""

    name: str
    description: str
    model: str
    roles: list[str]
    allow: list[str]
    deny: list[str]
    thinking: Thinking


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
        )


class AgentDefaultsOut(BaseModel):
    model: str
    # 新建 agent 默认的角色
    roles: list[str]


@router.get("/agent-defaults")
async def agent_defaults(c: C) -> AgentDefaultsOut:
    """新建 agent 时表单预填的默认值（与创建时套用的是同一份 AgentService.defaults）。"""
    d = c.agents.defaults
    return AgentDefaultsOut(model=d.model, roles=list(d.roles))


class PermissionOut(BaseModel):
    """一个工具的权限（agents.permissions.TOOL_PERMISSIONS）。"""

    tool: str
    id: str
    access: Literal["read", "write", "irreversible"]
    note: str

    @classmethod
    def of(cls, tool: str, p: Permission) -> "PermissionOut":
        return cls(tool=tool, id=p.id, access=p.access.value, note=p.note)


@router.get("/permissions")
async def list_permissions() -> list[PermissionOut]:
    """全部已登记工具的权限，按登记顺序（含 web，即使没配置 [web]）。MCP 工具是 mcp:<server>:<tool>，不在其中。"""
    return [PermissionOut.of(t, p) for t, p in TOOL_PERMISSIONS.items()]


class RulesIn(BaseModel):
    roles: list[str]
    allow: list[str]
    deny: list[str]


class ResolvedOut(BaseModel):
    """按规则最终能用的工具（不含 MCP），以及是否可能放行 MCP 工具；能开临时 agent 时另有临时 agent 按同一份规则能用的。"""

    tools: list[PermissionOut]
    mcp: bool
    temp_agent: "ResolvedOut | None"

    @classmethod
    def of(cls, r: Resolved) -> "ResolvedOut":
        return cls(
            tools=[PermissionOut.of(t, TOOL_PERMISSIONS[t]) for t in r.tools],
            mcp=r.mcp,
            temp_agent=None if r.temp_agent is None else cls.of(r.temp_agent),
        )


@router.post("/permissions/resolve")
async def resolve_permissions(c: C, body: RulesIn) -> ResolvedOut:
    """agent 配置（还没保存的也行）最终能用的工具：public + 角色 + allow / deny，与运行时同一套计算。"""
    return ResolvedOut.of(await c.agents.preview(body.roles, body.allow, body.deny))


class RoleOut(BaseModel):
    name: str
    description: str
    allow: list[str]
    deny: list[str]
    # 内置角色不能改、不能删；public 每个 agent 自动拥有
    builtin: bool
    # 只按这个角色自己的规则能用的工具
    resolved: ResolvedOut

    @classmethod
    def of(cls, r: Role, resolved: Resolved) -> "RoleOut":
        return cls(
            name=r.name,
            description=r.description,
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
    return [RoleOut.of(r, c.agents.resolve(r.rules)) for r in await c.roles.list()]


@router.post("/roles/{name}", status_code=201)
async def create_role(c: C, name: str, body: RoleIn) -> RoleOut:
    role = await c.roles.create(RoleSpec(name, body.description, body.allow, body.deny))
    return RoleOut.of(role, c.agents.resolve(role.rules))


@router.put("/roles/{name}")
async def update_role(c: C, name: str, body: RoleIn) -> RoleOut:
    """整体替换自建角色的描述与规则；从引用它的 agent 的下一次运行起生效。"""
    role = await c.roles.update(RoleSpec(name, body.description, body.allow, body.deny))
    return RoleOut.of(role, c.agents.resolve(role.rules))


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
    return MemoryOut(content=c.memory.read(owner), max_chars=c.memory.max_chars)


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


SKILL_PACKAGE_TYPE = "application/zip"


@router.get(
    "/skill/package",
    response_class=Response,
    responses={200: {"content": {SKILL_PACKAGE_TYPE: {}}, "description": "<name>.skill"}},
)
async def export_skill(c: C, name: str, owner: str | None = None) -> Response:
    """下载标准 .skill 文件（zip：`<name>/SKILL.md` 与版本管理里的附件）。"""
    await check_owner(c, owner)
    package = await c.skills.export(owner, name)
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
    """请求体是一个标准 .skill 文件，导入为 owner（缺省为公共）的 skill，一次提交、作者 user。同名 skill 已存在时 409，
    replace=true 覆盖。包的结构不对（路径越界、符号链接、顶层目录不唯一、名字与目录不一致……）422，超限 413。"""
    package = await read_limited(request, MAX_PACKAGE_BYTES)
    return SkillImportOut.of(await c.skills.import_package(owner, package, USER, replace=replace))
