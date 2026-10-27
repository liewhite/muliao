"""条目 owner 的变更（条目子域）：转移给别的 agent 或平台（transfer_in），agent 删除时处理它名下的条目（retire_owned_in）。
两处都要在不经各 store 的地方给条目记一版，所以条目行在版本表里的文档（document_in）也在这里：每种存在 items 表里的条目
（agents.kinds.StoredKind）一项，交给各 store 自己的写法（skills.document、mcp.document、secrets.document）。新增一种条目在
DOCUMENTS 加一项（tests/test_permissions.py 核对每种都有）。"""

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents import mcp, secrets, skills
from muliao.agents.access import Keep
from muliao.agents.items import owned_in, owner_label_in
from muliao.agents.kinds import Kind, StoredKind, as_subject, label
from muliao.agents.revisions import record_in
from muliao.agents.skill_package import SkillFile
from muliao.agents.tables import Item, ItemFile


async def _skill(s: AsyncSession, row: Item) -> str:
    files = await s.scalars(select(ItemFile).where(ItemFile.item_id == row.id))
    return skills.document(row.content, {f.path: SkillFile(f.content, f.executable) for f in files})


async def _mcp(_s: AsyncSession, row: Item) -> str:
    return mcp.document(mcp.McpConfig.model_validate_json(row.content), row.enabled)


async def _secret(_s: AsyncSession, row: Item) -> str:
    return secrets.document(row.description)


DOCUMENTS: dict[StoredKind, Callable[[AsyncSession, Item], Awaitable[str]]] = {
    StoredKind.SKILL: _skill,
    StoredKind.MCP: _mcp,
    StoredKind.SECRET: _secret,
}


async def document_in(s: AsyncSession, row: Item) -> str:
    return await DOCUMENTS[StoredKind(row.kind)](s, row)


async def transfer_in(
    s: AsyncSession, row: Item, owner: str | None, actor: str, message: str | None = None
) -> None:
    """把条目转给 owner（agent 的 id；None 归平台），在调用方的写事务里记一版（改的人 actor，说明缺省为「从谁转给谁」）。
    内容不变；谁能用随 owner 变（agents.access 的规则 1）。谁能转、新 owner 是否可用由调用方先查。"""
    kind = StoredKind(row.kind)
    if message is None:
        message = (
            f"transfer {label(kind)} {row.name} from {await owner_label_in(s, row.owner)} "
            f"to {await owner_label_in(s, owner)}"
        )
    doc = await document_in(s, row)
    # 文档不含 owner：前后相同，照样记一版
    record_in(s, as_subject(kind), row.name, doc, doc, actor, message, force=True)
    row.owner = owner


async def retire_owned_in(s: AsyncSession, agent_id: str, shared: Keep, actor: str, message: str) -> None:
    """agent 删除前处理它名下的条目：分发出去的（shared，agents.access.Sharing.shared）转给平台（别的 agent 还在用），其余
    记一版删除（行随 agent 的行级联删除，版本表留作审计）。转移先写下去，调用方随后删 agent 的行时级联只删还归它的。"""
    for row in await owned_in(s, agent_id):
        if shared(Kind(row.kind), row.name, agent_id):
            await transfer_in(s, row, None, actor, f"moved to the platform with agent {agent_id} deleted")
            continue
        record_in(
            s, as_subject(StoredKind(row.kind)), row.name, await document_in(s, row), None, actor, message
        )
    await s.flush()
