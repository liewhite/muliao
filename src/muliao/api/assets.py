"""AI 资源（内置工具、skill、MCP server、secret、记忆）的接口，按统一模型：`/assets` 列出全部资源（每种带自己的详情），
`/assets/{kind}/{name}` 读、写、删一个资源，`/owner` 对存成文件的资源通用（挪到另一个 owner 的目录），各种类专有的操作
在自己的子路径（MCP 的启停与测试连接、skill 包的导入导出）。资源都是资源目录里的文件（agents.assets），这里读写的就是它们。

这里的写入都是用户在界面上做的；新建的资源归请求给出的 owner（缺省为平台），已有的 owner 不变。
"""

import json
from datetime import datetime
from typing import Annotated, Literal

import anyio
from fastapi import APIRouter, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from muliao.agents.api import (
    MAX_PACKAGE_BYTES,
    USER,
    CatalogAsset,
    McpConfig,
    McpFile,
    McpServer,
    McpTransport,
    MemoryInfo,
    MemoryText,
    SecretDetail,
    SkillDir,
    SkillImport,
    SkillMeta,
    StoredKind,
    ToolSpec,
    mcp_config,
)
from muliao.api.deps import C, read_limited
from muliao.api.workspace import EntryOut, file_response
from muliao.workspace.api import file_in, list_dir

router = APIRouter()

# ---- 资产池 ----


class _CatalogBase(BaseModel):
    """资产池里的一项，在规则里写作 `<action>:<kind>:<name>`。"""

    name: str
    # agent 的 id；null 归平台（内置工具、界面上建的、迁移前的公共资产）
    owner: str | None
    description: str
    # 收录它的角色（allow 放行、deny 没拦，含内置的 base、admin）
    roles: list[str]
    # 分发出去了：base 收录它，或某个不是 owner 的 agent 的规则（角色或自己的 allow）放行它；否则只有 owner 能用
    # （归平台的就谁也用不了）。与删除 agent 时把资产转给平台同一个定义
    shared: bool


class CatalogToolOut(_CatalogBase):
    """内置工具：归平台、代码里定义，不能改。"""

    kind: Literal["tool"]
    # 代码里定义，没有修改时间
    updated_at: None
    access: Literal["read", "write"]
    # 管理类工具：base 角色没有
    admin: bool


class _StoredCatalogBase(_CatalogBase):
    # 文件的修改时间
    updated_at: datetime
    # 文件（skill 是目录）的位置
    path: str


class CatalogSkillOut(_StoredCatalogBase):
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
    def fields_of(cls, config: McpConfig, enabled: bool) -> dict:
        return {
            "transport": config.transport,
            "command": config.command,
            "args": config.args,
            "url": config.url,
            "env": config.env,
            "headers": config.headers,
            "enabled": enabled,
        }


class CatalogMcpOut(_StoredCatalogBase, McpConfigOut):
    kind: Literal["mcp"]


class CatalogSecretOut(_StoredCatalogBase):
    """值不返回。"""

    kind: Literal["secret"]
    # false：agent 自己写的命令不能注入，只有 skill_run 的 skill 脚本与 MCP 配置能用（#528）
    allow_shell: bool


class CatalogMemoryOut(_StoredCatalogBase):
    """正文不在列表里（GET /assets/memory/{name} 读）。"""

    kind: Literal["memory"]


CatalogAssetOut = Annotated[
    CatalogToolOut | CatalogSkillOut | CatalogMcpOut | CatalogSecretOut | CatalogMemoryOut,
    Field(discriminator="kind"),
]


def _asset_out(
    e: CatalogAsset,
) -> CatalogToolOut | CatalogSkillOut | CatalogMcpOut | CatalogSecretOut | CatalogMemoryOut:
    common = {
        "name": e.name,
        "owner": e.owner,
        "description": e.description,
        "updated_at": e.updated_at,
        "roles": list(e.roles),
        "shared": e.shared,
    }
    stored = {**common, "path": str(e.path)}
    match e.detail:
        case ToolSpec() as tool:
            return CatalogToolOut(kind="tool", access=tool.access.value, admin=tool.admin, **common)
        case SkillDir():
            return CatalogSkillOut(kind="skill", **stored)
        case McpFile(config=config, enabled=enabled):
            return CatalogMcpOut(kind="mcp", **stored, **McpConfigOut.fields_of(config, enabled))
        case SecretDetail(allow_shell=allow_shell):
            return CatalogSecretOut(kind="secret", allow_shell=allow_shell, **stored)
        case MemoryText():
            return CatalogMemoryOut(kind="memory", **stored)


@router.get("/assets")
async def list_assets(c: C) -> list[CatalogAssetOut]:
    """全部资源：内置工具（当前配置下注册的，按注册顺序）在前，存成文件的按种类、名称；每种带自己的
    详情（工具的读写与管理类、MCP 的配置与启停）。"""
    return [_asset_out(e) for e in await c.catalog.catalog()]


# ---- 存成文件的资源通用：删除、owner ----


@router.delete("/assets/{kind}/{name}", status_code=204)
async def delete_asset(c: C, kind: StoredKind, name: str) -> None:
    """删掉文件（skill 是整个目录）。全局记忆不能删（422）；内置工具不在这里（kind 不接受 tool）。"""
    await c.catalog.delete(kind, name)


class OwnerIn(BaseModel):
    # agent 的 id；null 归平台
    owner: str | None


@router.put("/assets/{kind}/{name}/owner", status_code=204)
async def transfer_asset(c: C, kind: StoredKind, name: str, body: OwnerIn) -> None:
    """挪到另一个 owner 的目录下；内容与收录它的角色不变。内置工具归平台，不在这里（kind 不接受 tool）。"""
    await c.catalog.transfer(kind, name, body.owner)


# ---- 记忆 ----


class MemoryOut(BaseModel):
    name: str
    owner: str | None
    content: str
    updated_at: datetime
    path: str
    # 每一块的上限
    max_chars: int
    # content 的版本：保存时交回
    version: str


class MemoryIn(BaseModel):
    # 新建时的 owner（缺省为平台）；已有的不变
    owner: str | None = None
    content: str
    # 打开编辑时的版本（MemoryOut.version），新建为 null；期间被别处改过、删掉或抢先建了时 409
    version: str | None


def _memory_out(c: C, m: MemoryInfo) -> MemoryOut:
    return MemoryOut(
        name=m.name,
        owner=m.owner,
        content=m.content,
        updated_at=m.updated_at,
        path=str(m.path),
        max_chars=c.memory.max_chars,
        version=m.version,
    )


@router.get("/assets/memory/{name}")
async def get_memory(c: C, name: str) -> MemoryOut:
    return _memory_out(c, await c.memory.read(name))


@router.put("/assets/memory/{name}")
async def put_memory(c: C, name: str, body: MemoryIn) -> MemoryOut:
    """新建的归 owner（归平台的名字要是 slug；agent 的私有记忆名字就是 agent 名）；已有的整体改写正文，owner 不变。谁能用由
    角色收录它（`use:memory:<名>`）。"""
    return _memory_out(c, await c.memory.put(name, body.content, version=body.version, owner=body.owner))


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
    # SKILL.md 的版本：保存时交回
    version: str


class SkillIn(BaseModel):
    # 新建时的 owner；已有的不变
    owner: str | None
    description: str
    body: str
    # 打开编辑时的版本（SkillDetail.version），新建为 null；期间被别处改过、删掉或抢先建了时 409
    version: str | None


@router.get("/assets/skill/{name}")
async def get_skill(c: C, name: str) -> SkillDetail:
    skill = await c.skills.read(name)
    meta = skill.meta
    return SkillDetail(
        name=meta.name, description=meta.description, owner=meta.owner, body=skill.body, version=skill.version
    )


@router.put("/assets/skill/{name}")
async def put_skill(c: C, name: str, body: SkillIn) -> SkillOut:
    """新建的归 owner；已有的整体改写正文与描述（附件不变），owner 不变。"""
    meta = await c.skills.write(name, body.description, body.body, owner=body.owner, version=body.version)
    return SkillOut.of(meta)


@router.get("/assets/skill/{name}/files")
async def list_skill_files(c: C, name: str, path: str = "") -> list[EntryOut]:
    """skill 目录里 path（相对 skill 目录，空为根）这一层，同工作目录浏览：目录在前，各自按名称排序。"""
    root = await c.skills.directory(name)
    return [EntryOut.of(e) for e in await anyio.to_thread.run_sync(list_dir, root, path)]


@router.get("/assets/skill/{name}/file")
async def read_skill_file(c: C, name: str, path: str) -> FileResponse:
    """skill 目录里一个文件的内容（同工作目录浏览：图片 inline，其余作为附件）。"""
    root = await c.skills.directory(name)
    return file_response(await anyio.to_thread.run_sync(file_in, root, path))


SKILL_PACKAGE_TYPE = "application/zip"
# 导出时没打进包的文件：JSON 数组（元素同 SkippedOut，非 ASCII 转义），没有时为 []
SKIPPED_HEADER = "X-Skipped-Files"


class SkippedOut(BaseModel):
    # 包里的路径（相对 skill 目录；macOS 元数据是包里的完整路径）
    path: str
    reason: str


@router.get(
    "/assets/skill/{name}/package",
    response_class=Response,
    responses={
        200: {
            "content": {SKILL_PACKAGE_TYPE: {}},
            "description": "<name>.skill",
            "headers": {
                SKIPPED_HEADER: {
                    "description": "没打进包的文件（二进制等）：JSON 数组，每项 {path, reason}",
                    "schema": {"type": "string"},
                }
            },
        }
    },
)
async def export_skill(c: C, name: str) -> Response:
    """下载标准 .skill 文件（zip：`<name>/SKILL.md` 与附件）；没打进包的文件同导入一样逐个列出（响应头 X-Skipped-Files）。"""
    exported = await c.skills.export(name)
    skipped = [SkippedOut(path=s.path, reason=s.reason).model_dump() for s in exported.skipped]
    return Response(
        exported.package,
        media_type=SKILL_PACKAGE_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{name}.skill"',
            # 头只能是 latin-1：json.dumps 默认把非 ASCII 字符转义
            SKIPPED_HEADER: json.dumps(skipped),
        },
    )


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


@router.post("/assets/skill")
async def import_skill(
    c: C, request: Request, owner: str | None = None, replace: bool = False
) -> SkillImportOut:
    """请求体是一个标准 .skill 文件，导入为 skill（名字取自包；新建的归 owner，缺省为平台），一次提交、改的人 user。同名
    skill 已存在时 409，replace=true 覆盖。包的结构不对（路径越界、符号链接、顶层目录不唯一、名字与目录不一致……）422，
    超限 413。"""
    package = await read_limited(request, MAX_PACKAGE_BYTES)
    return SkillImportOut.of(await c.skills.import_package(package, owner=owner, replace=replace))


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
        return cls(
            owner=m.owner, name=m.name, tools=tools or [], **McpConfigOut.fields_of(m.config, m.enabled)
        )


class EnabledIn(BaseModel):
    enabled: bool


@router.get("/assets/mcp/{name}")
async def get_mcp(c: C, name: str) -> McpOut:
    return McpOut.of(await c.mcp.get(name))


@router.put("/assets/mcp/{name}", status_code=201)
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
    server, tools = await c.mcp.add(name, config, owner=body.owner, probe=body.probe)
    return McpOut.of(server, tools)


@router.put("/assets/mcp/{name}/enabled")
async def set_mcp_enabled(c: C, name: str, body: EnabledIn) -> McpOut:
    return McpOut.of(await c.mcp.set_enabled(name, body.enabled))


@router.post("/assets/mcp/{name}/tools")
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


@router.get("/assets/secret/{name}")
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


@router.put("/assets/secret/{name}", status_code=204)
async def set_secret(c: C, name: str, body: SecretIn) -> None:
    """新建的归 owner（缺省为平台）；已有的改值与描述，owner 不变。"""
    await c.vault.set(
        name, body.value, body.description, actor=USER, owner=body.owner, allow_shell=body.allow_shell
    )
