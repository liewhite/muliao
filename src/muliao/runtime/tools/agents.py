"""agent 目录：查看有哪些 agent。给模型看的是名称和描述，不出现 id。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset

from muliao.agents import AgentService
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES


class AgentInfo(BaseModel):
    name: str
    description: str


def agents_toolset(agents: AgentService) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentInfo]:
        """List all agents and what each one is for. Use before delegating."""
        return [AgentInfo(name=a.name, description=a.description) for a in await agents.list()]

    return ts
