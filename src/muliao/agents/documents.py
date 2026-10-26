"""一个条目行在版本表里的文档，按种类分发到各 store 自己的写法（skills.document、mcp.document、secrets.document）。
给不经各 store 的删除用（agent 删除时它名下的条目随行级联删除，AgentLifecycle）；新增一种条目在这里加一行。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents import mcp, secrets, skills
from muliao.agents.revisions import Subject
from muliao.agents.skill_package import SkillFile
from muliao.agents.tables import Item, ItemFile


async def document_in(s: AsyncSession, row: Item) -> str:
    match Subject(row.kind):
        case Subject.SKILL:
            files = await s.scalars(select(ItemFile).where(ItemFile.item_id == row.id))
            return skills.document(row.content, {f.path: SkillFile(f.content, f.executable) for f in files})
        case Subject.MCP:
            return mcp.document(mcp.McpConfig.model_validate_json(row.content), row.enabled)
        case Subject.SECRET:
            return secrets.document(row.description, row.updated_at)
        case other:
            raise RuntimeError(f"item {row.name!r} has unknown kind {other}")
