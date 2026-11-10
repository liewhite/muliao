from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from muliao.agent_runtime.api import memory_section
from muliao.agents.api import (
    USER,
    AgentInfo,
    AgentPatch,
    AgentRules,
    AgentSpec,
    McpTransport,
    Reach,
    RoleSpec,
    RoleView,
    Source,
    StoredKind,
    Thinking,
    ToolSpec,
    UsedAsset,
)
from muliao.api.deps import C

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
    def of(cls, a: AgentInfo, rules: AgentRules) -> "AgentOut":
        return cls(
            id=a.id,
            name=a.name,
            description=a.description,
            model=a.model,
            roles=list(rules.roles),
            allow=list(rules.allow),
            deny=list(rules.deny),
            thinking=a.thinking,
            created_at=a.created_at,
            is_default=a.is_default,
            group=a.group,
            archived_at=a.archived_at,
        )


async def _out(c: C, a: AgentInfo) -> AgentOut:
    """操作之后的 agent：只取它的 id，目录字段与分发规则在同一个事务里重新读（两个子域各自的读模型）。"""
    return AgentOut.of(*await c.agents.get_with_rules(a.id))


@router.get("/agent-defaults")
async def agent_defaults(c: C) -> AgentUpdate:
    """新建 agent 时表单预填的值：创建时套用的同一份缺省（AgentSpec 的字段缺省，模型取 AgentService.defaults），
    前端不另写一份。"""
    spec = AgentSpec(name="", description="")
    return AgentUpdate(
        name=spec.name,
        description=spec.description,
        model=c.agents.defaults.model,
        roles=list(spec.roles),
        allow=list(spec.allow),
        deny=list(spec.deny),
        thinking=spec.thinking,
        group=spec.group,
    )


class ToolOut(BaseModel):
    """一个内置工具（agents.permissions.TOOLS 里登记的），在规则里写作 `tool:<name>`。"""

    name: str
    access: Literal["read", "write"]
    # 管理类工具：base 角色没有
    admin: bool

    @classmethod
    def of(cls, t: ToolSpec) -> "ToolOut":
        return cls(name=t.name, access=t.access.value, admin=t.admin)


class ReachOut(BaseModel):
    """一份配置能用（use）的内置工具与资产（skill、MCP server、secret、记忆，`kind:name`）。"""

    tools: list[ToolOut]
    assets: list[str]

    @classmethod
    def of(cls, r: Reach) -> "ReachOut":
        return cls(tools=[ToolOut.of(t) for t in r.tools], assets=r.assets)


class AgentPreviewIn(BaseModel):
    # 已有的 agent（它自己的资产总能用）；null 为还没建的
    agent_id: str | None
    roles: list[str]
    allow: list[str]
    deny: list[str]


@router.post("/agent-preview")
async def preview_agent(c: C, body: AgentPreviewIn) -> ReachOut:
    """agent 配置（还没保存的也行）最终能用的：自己的资产 + base + 角色 + allow / deny 放行的，与运行时同一套计算，
    与保存时同样校验（不合法 422）。"""
    rules = AgentRules.of(body.roles, body.allow, body.deny)
    return ReachOut.of(await c.distribution.preview_agent(body.agent_id, rules))


class RoleOut(BaseModel):
    name: str
    description: str
    # 内置角色（base、admin）代码里固定的规则，不可改
    fixed_allow: list[str]
    # 可编辑的规则
    allow: list[str]
    deny: list[str]
    # 内置角色不能删
    builtin: bool
    # 能写进 agent 的 roles：base 每个 agent 隐含，不能选
    assignable: bool
    # 只按这个角色的规则（固定的与可编辑的）能用的
    reach: ReachOut
    # 带这个角色的 agent 的 id（含归档的）；base 是全部 agent
    agents: list[str]

    @classmethod
    def of(cls, v: RoleView) -> "RoleOut":
        r = v.role
        return cls(
            name=r.name,
            description=r.description,
            fixed_allow=list(r.fixed.allow),
            allow=list(r.rules.allow),
            deny=list(r.rules.deny),
            builtin=r.builtin,
            assignable=r.assignable,
            reach=ReachOut.of(v.reach),
            agents=list(v.holders),
        )


class RoleIn(BaseModel):
    description: str
    allow: list[str]
    deny: list[str]


@router.get("/roles")
async def list_roles(c: C) -> list[RoleOut]:
    """全部角色：内置在前，自建的按名称。"""
    return [RoleOut.of(v) for v in await c.distribution.views()]


class RolePreviewIn(BaseModel):
    # 已有的角色（内置的加上固定规则）；null 为还没建的
    name: str | None
    allow: list[str]
    deny: list[str]


@router.post("/role-preview")
async def preview_role(c: C, body: RolePreviewIn) -> ReachOut:
    """角色规则（还没保存的也行）能用的，与保存时同样校验（不合法 422）。"""
    return ReachOut.of(await c.distribution.preview_role(body.name, body.allow, body.deny))


@router.post("/roles/{name}", status_code=201)
async def create_role(c: C, name: str, body: RoleIn) -> RoleOut:
    await c.distribution.create(RoleSpec(name, body.description, body.allow, body.deny), actor=USER)
    return RoleOut.of(await c.distribution.view(name))


@router.put("/roles/{name}")
async def update_role(c: C, name: str, body: RoleIn) -> RoleOut:
    """整体替换角色的描述与可编辑的规则（内置角色也可以，固定的部分不变）；从带它的 agent 的下一次运行起生效。"""
    await c.distribution.update(RoleSpec(name, body.description, body.allow, body.deny), actor=USER)
    return RoleOut.of(await c.distribution.view(name))


@router.delete("/roles/{name}", status_code=204)
async def delete_role(c: C, name: str) -> None:
    """有 agent 引用时 409。"""
    await c.distribution.delete(name, actor=USER)


@router.get("/agents")
async def list_agents(c: C) -> list[AgentOut]:
    return [AgentOut.of(a, rules) for a, rules in await c.agents.list_with_rules()]


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
    return await _out(c, await c.agents.create(spec, actor=USER))


@router.get("/agents/{agent_id}")
async def get_agent(c: C, agent_id: str) -> AgentOut:
    # 正在删除的按不存在处理
    return AgentOut.of(*await c.agents.get_with_rules(agent_id))


class AssetSourceOut(BaseModel):
    """为什么能用：它名下的、放行它的角色（含 base）、它自己的 allow 规则（至少一项）。"""

    own: bool
    roles: list[str]
    allow: bool

    @classmethod
    def of(cls, s: Source) -> "AssetSourceOut":
        return cls(own=s.own, roles=list(s.roles), allow=s.allow)


class AssetOut(BaseModel):
    name: str
    # null 归平台
    owner: str | None
    description: str
    source: AssetSourceOut
    # 它能改（write）
    editable: bool

    @classmethod
    def of(cls, u: UsedAsset) -> "AssetOut":
        return cls(
            name=u.name,
            owner=u.owner,
            description=u.description,
            source=AssetSourceOut.of(u.source),
            editable=u.editable,
        )


class McpAssetOut(AssetOut):
    transport: McpTransport
    enabled: bool


class AgentAssetsOut(BaseModel):
    """agent 能用的资产，按种类分组，各组按名称。"""

    memories: list[AssetOut]
    # 记忆注入 instructions 的那一段（与运行时同一个渲染函数）
    memory_preview: str
    skills: list[AssetOut]
    secrets: list[AssetOut]
    mcp: list[McpAssetOut]


@router.get("/agents/{agent_id}/assets")
async def agent_assets(c: C, agent_id: str) -> AgentAssetsOut:
    """agent 能用的记忆、skill、secret、MCP server 与各自的来源（不含 secret 的值），加上记忆合并后放进指令的样子。"""
    used = await c.distribution.assets(agent_id)
    by_kind = {k: [u for u in used if u.kind == k] for k in StoredKind}
    preview = memory_section(await c.memory.for_run(agent_id), c.memory.max_chars)
    return AgentAssetsOut(
        memories=[AssetOut.of(u) for u in by_kind[StoredKind.MEMORY]],
        memory_preview=preview,
        skills=[AssetOut.of(u) for u in by_kind[StoredKind.SKILL]],
        secrets=[AssetOut.of(u) for u in by_kind[StoredKind.SECRET]],
        mcp=[_mcp_out(u) for u in by_kind[StoredKind.MCP]],
    )


def _mcp_out(u: UsedAsset) -> McpAssetOut:
    if u.mcp is None:
        raise RuntimeError(f"MCP asset {u.name!r} has no server config")
    return McpAssetOut(**AssetOut.of(u).model_dump(), transport=u.mcp.transport, enabled=u.mcp.enabled)


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
    return await _out(c, await c.agents.update(agent_id, patch, actor=USER))


@router.put("/agents/{agent_id}/default")
async def set_default_agent(c: C, agent_id: str) -> AgentOut:
    """设为系统默认 agent（取代原来的默认）。"""
    return await _out(c, await c.agents.set_default(agent_id, True))


@router.delete("/agents/{agent_id}/default")
async def unset_default_agent(c: C, agent_id: str) -> AgentOut:
    """取消它的系统默认身份；它不是默认时不做任何事。"""
    return await _out(c, await c.agents.set_default(agent_id, False))


@router.put("/agents/{agent_id}/archive")
async def archive_agent(c: C, agent_id: str) -> AgentOut:
    """归档：不能再对话、委派、指派，它的触发器不触发；它忙或是默认 agent 时 409。"""
    return await _out(c, await c.agent_lifecycle.set_archived(agent_id, True))


@router.delete("/agents/{agent_id}/archive")
async def unarchive_agent(c: C, agent_id: str) -> AgentOut:
    """取消归档；没归档时不做任何事。"""
    return await _out(c, await c.agent_lifecycle.set_archived(agent_id, False))


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(c: C, agent_id: str) -> None:
    await c.agent_lifecycle.delete(agent_id, actor=USER)
