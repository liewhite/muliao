"""agent 的目录与管理：查看、创建、修改、删除 agent，以及读写公共 prompt。

给模型看的是名称和描述，不出现 id。指令和公共 prompt 的每次修改都记在版本历史里（作者是做出修改的 agent），
用户可以在界面上回滚。
"""

from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_lifecycle import AgentLifecycle
from muliao.agents import AgentService, AgentSpec
from muliao.evolution.prompts import PromptStore
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES


@dataclass(frozen=True)
class AgentDefaults:
    """新建 agent 时未指定的字段取服务端配置（与 API 的默认值同一来源）。"""

    model: str
    reflect_every: int


class AgentInfo(BaseModel):
    name: str
    description: str


class AgentDetail(BaseModel):
    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int


def agents_toolset(
    agents: AgentService, lifecycle: AgentLifecycle, prompts: PromptStore, defaults: AgentDefaults
) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentInfo]:
        """List all agents and what each one is for. Use before delegating."""
        return [AgentInfo(name=a.name, description=a.description) for a in await agents.list()]

    @ts.tool_plain
    async def get_agent(agent: str) -> AgentDetail:
        """Show an agent's full configuration, including its instructions. Use your own name to see yours.

        Args:
            agent: Agent name from `list_agents`.
        """
        target = await agents.find_by_name(agent)
        return AgentDetail(
            name=target.name,
            description=target.description,
            model=target.model,
            instructions=await prompts.current(target.id),
            reflect_every=target.reflect_every,
        )

    @ts.tool
    async def create_agent(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        instructions: str,
        model: str | None = None,
        reflect_every: int | None = None,
    ) -> str:
        """Create a new long-lived agent with its own memory, skills and workspace, e.g. a specialist you will
        delegate to repeatedly. For a one-off job use `run_temp_agent` instead.

        Args:
            name: Display name, unique among agents.
            description: What it is for; other agents choose whom to delegate to by this.
            instructions: Its system prompt.
            model: `<provider>:<model>`; defaults to the server's default model.
            reflect_every: Turns between forced reflections; defaults to the server's setting.
        """
        spec = AgentSpec(
            name=name,
            description=description,
            model=defaults.model if model is None else model,
            instructions=instructions,
            reflect_every=defaults.reflect_every if reflect_every is None else reflect_every,
        )
        created = await agents.create(spec, author=ctx.deps.agent_id)
        return f"created agent {created.name}"

    @ts.tool
    async def update_agent(
        ctx: RunContext[RunScope],
        agent: str,
        new_name: str | None = None,
        description: str | None = None,
        instructions: str | None = None,
        model: str | None = None,
        reflect_every: int | None = None,
    ) -> str:
        """Change an agent's configuration, including your own (use your own name). Only the given fields change.
        Instructions replace the whole system prompt: read it first with `get_agent`. Every change is kept in a
        version history the user can roll back.

        Args:
            agent: Name of the agent to change.
            new_name: New display name.
            description: New description.
            instructions: New full system prompt.
            model: New `<provider>:<model>`.
            reflect_every: New reflection interval in turns.
        """
        target = await agents.find_by_name(agent)
        spec = AgentSpec(
            name=target.name if new_name is None else new_name,
            description=target.description if description is None else description,
            model=target.model if model is None else model,
            instructions=await prompts.current(target.id) if instructions is None else instructions,
            reflect_every=target.reflect_every if reflect_every is None else reflect_every,
        )
        updated = await agents.update(target.id, spec, author=ctx.deps.agent_id)
        return f"updated agent {updated.name}"

    @ts.tool_plain
    async def delete_agent(agent: str) -> str:
        """Delete an agent with its sessions, memory, skills, secrets and workspace. Irreversible.

        Args:
            agent: Name of the agent to delete.
        """
        target = await agents.find_by_name(agent)
        await lifecycle.delete(target.id)
        return f"deleted agent {target.name}"

    @ts.tool_plain
    async def get_public_prompt() -> str:
        """Show the public prompt: shared instructions placed before every agent's own instructions."""
        return await prompts.current(None) or "(no public prompt)"

    @ts.tool
    async def set_public_prompt(ctx: RunContext[RunScope], content: str) -> str:
        """Replace the public prompt shared by every agent. Read it first with `get_public_prompt`; keep it to rules
        that truly apply to all agents. Every change is kept in a version history the user can roll back.

        Args:
            content: The new full public prompt; empty removes it.
        """
        revision = await prompts.set(None, content, ctx.deps.agent_id)
        return "public prompt unchanged" if revision is None else "public prompt updated"

    return ts
