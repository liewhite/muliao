"""agent 的目录与管理：查看、创建、修改、删除 agent。给模型看的是名称和描述，不出现 id。"""

from collections.abc import Mapping, Sequence

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.lifecycle import AgentLifecycle
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
    tool_groups: list[str]
    # 所有可选的工具组，各带它包含的工具
    available_tool_groups: list[ToolGroup]


def agents_toolset(
    agents: AgentService,
    lifecycle: AgentLifecycle,
    group_tools: Mapping[str, Sequence[str]],
) -> FunctionToolset[RunScope]:
    """group_tools：除本组以外每个工具组包含的工具（组合根从各组实际注册的工具集取）；本组的工具在这里定义，自己补上。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def get_agent(agent: str) -> AgentDetail:
        """Show an agent's configuration and every available tool group. Use your own name to see yours.

        Args:
            agent: Agent name.
        """
        target = await agents.find_by_name(agent)
        return AgentDetail(
            name=target.name,
            description=target.description,
            model=target.model,
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
        model: str | None = None,
        tool_groups: list[str] | None = None,
    ) -> str:
        """Create a new long-lived agent with its own memory, skills and workspace, e.g. a specialist you will
        delegate to repeatedly. For a one-off job use a temporary agent (`run_temp_agent`, if you have it) instead.

        Args:
            name: Display name, unique among agents.
            description: What it is for (its role); it is shown to the agent itself and other agents choose
                whom to delegate to by it. The agent starts with an empty memory and grows it by itself.
            model: `<provider>:<model>`; defaults to the server's default model.
            tool_groups: Tool groups it may use (`get_agent` lists every available group with its tools);
                defaults to all. Give an agent that handles untrusted input (web pages, external events) only
                the groups whose tools it needs.
        """
        spec = AgentSpec(
            name=name,
            description=description,
            model=model,
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
        model: str | None = None,
        tool_groups: list[str] | None = None,
    ) -> str:
        """Change an agent's configuration, including your own (use your own name). Only the given fields change.

        Args:
            agent: Name of the agent to change.
            new_name: New display name.
            description: New description.
            model: New `<provider>:<model>`.
            tool_groups: New full list of tool groups it may use.
        """
        target = await agents.find_by_name(agent)
        patch = AgentPatch(
            name=new_name,
            description=description,
            model=model,
            tool_groups=tool_groups,
        )
        updated = await agents.update(target.id, patch)
        return f"updated agent {updated.name}"

    @ts.tool
    async def delete_agent(ctx: RunContext[RunScope], agent: str) -> str:
        """Delete another agent with its sessions, memory, skills, private secrets and MCP servers, triggers
        and workspace. Irreversible. Threads its conversations opened with other agents that the user
        can see are kept as standalone sessions; threads it was working on for others are deleted, and any
        unfinished work in them is reported back to its delegator as deleted. You cannot delete yourself.

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

    return ts
