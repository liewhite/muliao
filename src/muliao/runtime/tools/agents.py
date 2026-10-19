"""agent 的目录与管理：查看、创建、修改、删除 agent，以及读写公共 prompt。

给模型看的是名称和描述，不出现 id。指令和公共 prompt 的每次修改都记在版本历史里（作者是做出修改的 agent），
用户可以在界面上回滚。
"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_lifecycle import AgentLifecycle
from muliao.agents import AgentPatch, AgentService, AgentSpec
from muliao.errors import InvalidInput
from muliao.evolution.prompts import PromptStore
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES


class AgentDetail(BaseModel):
    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int
    tool_groups: list[str]
    # 所有可选的工具组名称
    available_tool_groups: list[str]


def agents_toolset(
    agents: AgentService, lifecycle: AgentLifecycle, prompts: PromptStore
) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

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
            instructions=prompts.current(target.id),
            reflect_every=target.reflect_every,
            tool_groups=target.tool_groups,
            available_tool_groups=list(agents.defaults.tool_groups),
        )

    @ts.tool
    async def create_agent(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        instructions: str,
        model: str | None = None,
        reflect_every: int | None = None,
        tool_groups: list[str] | None = None,
    ) -> str:
        """Create a new long-lived agent with its own memory, skills and workspace, e.g. a specialist you will
        delegate to repeatedly. For a one-off job use `run_temp_agent` instead.

        Args:
            name: Display name, unique among agents.
            description: What it is for; other agents choose whom to delegate to by this.
            instructions: Its system prompt.
            model: `<provider>:<model>`; defaults to the server's default model.
            reflect_every: Turns between forced reflections; defaults to the server's setting.
            tool_groups: Tool groups it may use (`get_agent` lists the available ones); defaults to all.
                Give an agent that handles untrusted input (web pages, external events) only what it needs:
                workspace, temp_agent, delegation, agents, triggers and mcp_admin each amount to full access.
        """
        spec = AgentSpec(
            name=name,
            description=description,
            instructions=instructions,
            model=model,
            reflect_every=reflect_every,
            tool_groups=tool_groups,
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
        tool_groups: list[str] | None = None,
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
            tool_groups: New full list of tool groups it may use.
        """
        target = await agents.find_by_name(agent)
        patch = AgentPatch(
            name=new_name,
            description=description,
            model=model,
            instructions=instructions,
            reflect_every=reflect_every,
            tool_groups=tool_groups,
        )
        updated = await agents.update(target.id, patch, author=ctx.deps.agent_id)
        return f"updated agent {updated.name}"

    @ts.tool
    async def delete_agent(ctx: RunContext[RunScope], agent: str) -> str:
        """Delete another agent with its sessions, memory, skills, private secrets and MCP servers, triggers
        and workspace. Irreversible. Threads it was given by other conversations that the user can
        see are kept as standalone sessions. You cannot delete yourself.

        Args:
            agent: Name of the agent to delete.
        """
        target = await agents.find_by_name(agent)
        if target.id == ctx.deps.agent_id:
            raise InvalidInput(
                "you cannot delete yourself while you are running; ask the user to do it in the app"
            )
        await lifecycle.delete(target.id, author=ctx.deps.agent_id)
        return f"deleted agent {target.name}"

    @ts.tool_plain
    async def get_public_prompt() -> str:
        """Show the public prompt: shared instructions placed before every agent's own instructions."""
        return prompts.current(None) or "(no public prompt)"

    @ts.tool
    async def set_public_prompt(ctx: RunContext[RunScope], content: str) -> str:
        """Replace the public prompt shared by every agent. Read it first with `get_public_prompt`; keep it to rules
        that truly apply to all agents. Every change is kept in a version history the user can roll back.

        Args:
            content: The new full public prompt; empty removes it.
        """
        changed = await prompts.set(None, content, ctx.deps.agent_id)
        return "public prompt updated" if changed else "public prompt unchanged"

    return ts
