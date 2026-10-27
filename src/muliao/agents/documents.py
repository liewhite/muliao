"""一个条目行在版本表里的文档：每种存在 items 表里的条目（agents.kinds.StoredKind）一项，交给各 store 自己的写法
（skills.document、mcp.document、secrets.document）。给不经各 store 写文档的地方用：转移 owner、随 agent 删除或转给平台
（AgentLifecycle）。新增一种条目在 DOCUMENTS 加一项（tests/test_permissions.py 核对每种都有）。"""

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents import mcp, secrets, skills
from muliao.agents.kinds import StoredKind
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
