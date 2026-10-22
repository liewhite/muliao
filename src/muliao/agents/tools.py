"""agent 的目录与管理：查看、创建、修改、删除 agent，以及读写公共 prompt。

给模型看的是名称和描述，不出现 id。指令和公共 prompt 的每次修改都记在版本历史里（作者是做出修改的 agent），
用户可以在界面上回滚。
"""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.prompts import PromptStore
from muliao.agents.service import AgentPatch, AgentService, AgentSpec
from muliao.agents.tool_catalog import AGENTS
from muliao.errors import InvalidInput
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope


class ToolGroup(BaseModel):
    name: str
    tools: list[str]


class AgentDetail(BaseModel):
    name: str
    description: str
    model: str
    instructions: str
    reflect_every: int
    tool_groups: list[str]
    # 所有可选的工具组，各带它包含的工具
    available_tool_groups: list[ToolGroup]


def agents_toolset(
    agents: AgentService,
    lifecycle: AgentLifecycle,
    prompts: PromptStore,
    group_tools: Mapping[str, Sequence[str]],
) -> FunctionToolset[RunScope]:
    """group_tools：除本组以外每个工具组包含的工具（组合根从各组实际注册的工具集取）；本组的工具在这里定义，自己补上。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def get_agent(agent: str) -> AgentDetail:
        """Show an agent's full configuration, including its instructions. Use your own name to see yours.

        Args:
            agent: Agent name.
        """
        target = await agents.find_by_name(agent)
        return AgentDetail(
            name=target.name,
            description=target.description,
            model=target.model,
            instructions=prompts.current(target.id),
            reflect_every=target.reflect_every,
            tool_groups=target.tool_groups,
            available_tool_groups=[
                ToolGroup(name=g, tools=list(ts.tools) if g == AGENTS else list(group_tools[g]))
                for g in agents.defaults.group_names
            ],
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
        delegate to repeatedly. For a one-off job use a temporary agent (`run_temp_agent`, if you have it) instead.

        Args:
            name: Display name, unique among agents.
            description: What it is for; other agents choose whom to delegate to by this.
            instructions: Its own instructions (the agent-specific part of its system prompt; the public
                prompt and run context are added separately).
            model: `<provider>:<model>`; defaults to the server's default model.
            reflect_every: Steps (model responses) between forced reflections; defaults to the server's setting.
            tool_groups: Tool groups it may use (`get_agent` lists every available group with its tools);
                defaults to all. Give an agent that handles untrusted input (web pages, external events) only
                the groups whose tools it needs.
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
        Instructions replace the agent's whole own instructions (the agent-specific part of its system prompt):
        read them first with `get_agent`. Every change is kept in a version history the user can roll back.

        Args:
            agent: Name of the agent to change.
            new_name: New display name.
            description: New description.
            instructions: New full instructions.
            model: New `<provider>:<model>`.
            reflect_every: New reflection interval in steps (model responses).
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
        and workspace. Irreversible. Threads its conversations opened with other agents that the user
        can see are kept as standalone sessions; threads it was working on for others are deleted, and whoever
        delegated them gets the outcome "deleted". You cannot delete yourself.

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
