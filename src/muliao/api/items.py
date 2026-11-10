"""AI 资源（内置工具、skill、MCP server、secret、记忆）的接口，按统一模型：`/items` 列出全部资源（每种带自己的详情），
`/items/{kind}/{name}` 读、写、删一个资源，`/owner` 对存成文件的资源通用（挪到另一个 owner 的目录），各种类专有的操作
在自己的子路径（MCP 的启停与测试连接、skill 包的导入导出）。资源都是资源目录里的文件（agents.assets），这里读写的就是它们。

写入的人（actor）都是 user；新建的资源归请求给出的 owner（缺省为平台），已有的 owner 不变。
"""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from muliao.agents.api import (
    MAX_PACKAGE_BYTES,
    USER,
    CatalogEntry,
    Kind,
    McpServer,
    McpTransport,
    MemoryInfo,
    SkillImport,
    SkillMeta,
    StoredKind,
    mcp_config,
)
from muliao.api.deps import C, read_limited

router = APIRouter()

# ---- 条目池 ----


class _ItemBase(BaseModel):
    """资产池里的一项，在规则里写作 `<action>:<kind>:<name>`。"""

    name: str
    # agent 的 id；null 归平台（内置工具、界面上建的、迁移前的公共条目）
    owner: str | None
    description: str
    # 收录它的角色（allow 放行、deny 没拦，含内置的 base、admin）
    roles: list[str]
    # 分发出去了：base 收录它，或某个不是 owner 的 agent 的规则（角色或自己的 allow）放行它；否则只有 owner 能用
    # （归平台的就谁也用不了）。与删除 agent 时把条目转给平台同一个定义
    shared: bool


class ToolItemOut(_ItemBase):
    """内置工具：归平台、代码里定义，不能改。"""

    kind: Literal["tool"]
    # 代码里定义，没有修改时间
    updated_at: None
    access: Literal["read", "write"]
    # 管理类工具：base 角色没有
    admin: bool


class _StoredItemBase(_ItemBase):
    # 文件的修改时间
    updated_at: datetime
    # 文件（skill 是目录）的位置
    path: str


class SkillItemOut(_StoredItemBase):
    kind: Literal["skill"]


class McpConfigOut(BaseModel):
    transport: McpTransport
    command: str | None
    args: list[str]
    url: str | None
    # env / headers 里的 secret 只以 ${vault:KEY} 引用形式存储，可以原样返回
    env: dict[str, str]
    headers: dict[str, str]
    enabled: bool

    @classmethod
    def fields_of(cls, m: McpServer) -> dict:
        return {
            "transport": m.transport,
            "command": m.config.command,
            "args": m.config.args,
            "url": m.config.url,
            "env": m.config.env,
            "headers": m.config.headers,
            "enabled": m.enabled,
        }


class McpItemOut(_StoredItemBase, McpConfigOut):
    kind: Literal["mcp"]


class SecretItemOut(_StoredItemBase):
    """值不返回。"""

    kind: Literal["secret"]
    # false：agent 自己写的命令不能注入，只有 skill_run 的 skill 脚本与 MCP 配置能用（#528）
    allow_shell: bool


class MemoryItemOut(_StoredItemBase):
    """正文不在列表里（GET /items/memory/{name} 读）。"""

    kind: Literal["memory"]


ItemOut = Annotated[
    ToolItemOut | SkillItemOut | McpItemOut | SecretItemOut | MemoryItemOut, Field(discriminator="kind")
]


def _item_out(e: CatalogEntry) -> ToolItemOut | SkillItemOut | McpItemOut | SecretItemOut | MemoryItemOut:
    common = {
        "name": e.name,
        "owner": e.owner,
        "description": e.description,
        "updated_at": e.updated_at,
        "roles": list(e.roles),
        "shared": e.shared,
    }
    stored = {**common, "path": str(e.path)}
    match e.kind:
        case Kind.TOOL:
            tool = e.tool
            assert tool is not None
            return ToolItemOut(kind="tool", access=tool.access.value, admin=tool.admin, **common)
        case Kind.SKILL:
            return SkillItemOut(kind="skill", **stored)
        case Kind.MCP:
            assert e.mcp is not None
            return McpItemOut(kind="mcp", **stored, **McpConfigOut.fields_of(e.mcp))
        case Kind.SECRET:
            assert e.allow_shell is not None
            return SecretItemOut(kind="secret", allow_shell=e.allow_shell, **stored)
        case Kind.MEMORY:
            return MemoryItemOut(kind="memory", **stored)


@router.get("/items")
async def list_items(c: C) -> list[ItemOut]:
    """全部资源：内置工具（按登记顺序，仅当前配置下注册的）在前，存成文件的按种类、名称；每种带自己的
    详情（工具的读写与管理类、MCP 的配置与启停）。"""
    return [_item_out(e) for e in await c.catalog.catalog()]


# ---- 存成文件的资源通用：owner ----


class OwnerIn(BaseModel):
    # agent 的 id；null 归平台
    owner: str | None


@router.put("/items/{kind}/{name}/owner", status_code=204)
async def transfer_item(c: C, kind: StoredKind, name: str, body: OwnerIn) -> None:
    """挪到另一个 owner 的目录下；内容与收录它的角色不变。内置工具归平台，不在这里（kind 不接受 tool）。"""
    await c.catalog.transfer_item(kind, name, body.owner, USER)


# ---- 记忆 ----


class MemoryOut(BaseModel):
    name: str
    owner: str | None
    content: str
    updated_at: datetime
    path: str
    # 每一块的上限
    max_chars: int


class MemoryIn(BaseModel):
    # 新建时的 owner（缺省为平台）；已有的不变
    owner: str | None = None
    content: str


def _memory_out(c: C, m: MemoryInfo) -> MemoryOut:
    return MemoryOut(
        name=m.name,
        owner=m.owner,
        content=m.content,
        updated_at=m.updated_at,
        path=str(m.path),
        max_chars=c.memory.max_chars,
    )


@router.get("/items/memory/{name}")
async def get_memory(c: C, name: str) -> MemoryOut:
    return _memory_out(c, await c.memory.read(name))


@router.put("/items/memory/{name}")
async def put_memory(c: C, name: str, body: MemoryIn) -> MemoryOut:
    """新建的归 owner（归平台的名字要是 slug；agent 的私有记忆名字就是 agent 名）；已有的整体改写正文，owner 不变。谁能用由
    角色收录它（`use:memory:<名>`）。"""
    return _memory_out(c, await c.memory.put(name, body.content, actor=USER, owner=body.owner))


@router.delete("/items/memory/{name}", status_code=204)
async def delete_memory(c: C, name: str) -> None:
    """全局记忆不能删（422）。"""
    await c.memory.delete(name, USER)


# ---- skill ----


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
    # 新建时的 owner；已有的不变
    owner: str | None
    description: str
    body: str


@router.get("/items/skill/{name}")
async def get_skill(c: C, name: str) -> SkillDetail:
    skill = await c.skills.read(name)
    meta = skill.meta
    return SkillDetail(name=meta.name, description=meta.description, owner=meta.owner, body=skill.body)


@router.put("/items/skill/{name}")
async def put_skill(c: C, name: str, body: SkillIn) -> SkillOut:
    """新建的归 owner；已有的整体改写正文与描述（附件不变），owner 不变。"""
    meta = await c.skills.write(name, body.description, body.body, actor=USER, owner=body.owner)
    return SkillOut.of(meta)


@router.delete("/items/skill/{name}", status_code=204)
async def delete_skill(c: C, name: str) -> None:
    await c.skills.delete(name, USER)


SKILL_PACKAGE_TYPE = "application/zip"


@router.get(
    "/items/skill/{name}/package",
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


@router.post("/items/skill")
async def import_skill(
    c: C, request: Request, owner: str | None = None, replace: bool = False
) -> SkillImportOut:
    """请求体是一个标准 .skill 文件，导入为 skill（名字取自包；新建的归 owner，缺省为平台），一次提交、改的人 user。同名
    skill 已存在时 409，replace=true 覆盖。包的结构不对（路径越界、符号链接、顶层目录不唯一、名字与目录不一致……）422，
    超限 413。"""
    package = await read_limited(request, MAX_PACKAGE_BYTES)
    return SkillImportOut.of(await c.skills.import_package(package, actor=USER, owner=owner, replace=replace))


# ---- MCP server ----


class McpIn(BaseModel):
    owner: str | None
    transport: McpTransport
    command: str | None = None
    args: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    # 先连接一次验证（按用户能用的 secret 解析引用），连不上 422、不保存
    probe: bool = True


class McpOut(McpConfigOut):
    owner: str | None
    name: str
    # 新增时连接验证列出的工具（不验证时为空）
    tools: list[str] = []

    @classmethod
    def of(cls, m: McpServer, tools: list[str] | None = None) -> "McpOut":
        return cls(owner=m.owner, name=m.name, tools=tools or [], **McpConfigOut.fields_of(m))


class EnabledIn(BaseModel):
    enabled: bool


@router.get("/items/mcp/{name}")
async def get_mcp(c: C, name: str) -> McpOut:
    return McpOut.of(await c.mcp.get(name))


@router.put("/items/mcp/{name}", status_code=201)
async def add_mcp(c: C, name: str, body: McpIn) -> McpOut:
    """新增（启用的）server；同名的已存在 409（名字全局唯一，配置不能原地改写：删了再建）。"""
    config = mcp_config(
        transport=body.transport,
        command=body.command,
        args=list(body.args),
        url=body.url,
        env=dict(body.env),
        headers=dict(body.headers),
    )
    server, tools = await c.mcp.add(name, config, actor=USER, owner=body.owner, probe=body.probe)
    return McpOut.of(server, tools)


@router.delete("/items/mcp/{name}", status_code=204)
async def delete_mcp(c: C, name: str) -> None:
    await c.mcp.remove(name, USER)


@router.put("/items/mcp/{name}/enabled")
async def set_mcp_enabled(c: C, name: str, body: EnabledIn) -> McpOut:
    return McpOut.of(await c.mcp.set_enabled(name, body.enabled, USER))


@router.post("/items/mcp/{name}/tools")
async def probe_mcp(c: C, name: str) -> list[str]:
    """连接一次，列出它的工具（测试连接）。"""
    return await c.mcp.probe(await c.mcp.get(name), USER)


# ---- secret ----


class SecretOut(BaseModel):
    owner: str | None
    name: str
    description: str
    allow_shell: bool
    updated_at: datetime


class SecretIn(BaseModel):
    # 新建时的 owner；已有的不变
    owner: str | None = None
    value: str
    description: str = ""
    # None：已有的保持原值，新建的允许（Vault.set）
    allow_shell: bool | None = None


@router.get("/items/secret/{name}")
async def get_secret(c: C, name: str) -> SecretOut:
    """不含值。"""
    s = await c.vault.get(name)
    return SecretOut(
        owner=s.owner,
        name=s.key,
        description=s.description,
        allow_shell=s.allow_shell,
        updated_at=s.updated_at,
    )


@router.put("/items/secret/{name}", status_code=204)
async def set_secret(c: C, name: str, body: SecretIn) -> None:
    """新建的归 owner（缺省为平台）；已有的改值与描述，owner 不变。"""
    await c.vault.set(
        name, body.value, body.description, actor=USER, owner=body.owner, allow_shell=body.allow_shell
    )


@router.delete("/items/secret/{name}", status_code=204)
async def delete_secret(c: C, name: str) -> None:
    await c.vault.delete(name, USER)
