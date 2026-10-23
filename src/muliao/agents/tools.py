"""agent 的目录与管理：查看、创建、修改、删除 agent。给模型看的是名称和描述，不出现 id。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.permissions import TOOL_PERMISSIONS, Permission
from muliao.agents.roles import RoleService
from muliao.agents.service import AgentPatch, AgentService, AgentSpec, Thinking
from muliao.errors import InvalidInput
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope


class ToolPermission(BaseModel):
    tool: str
    permission: str
    access: str

    @classmethod
    def of(cls, tool: str, p: Permission) -> "ToolPermission":
        return cls(tool=tool, permission=p.id, access=p.access)


class RoleDetail(BaseModel):
    name: str
    description: str
    allow: list[str]
    deny: list[str]


class AgentDetail(BaseModel):
    name: str
    description: str
    model: str
    thinking: Thinking
    roles: list[str]
    allow: list[str]
    deny: list[str]
    # 按角色 + allow/deny 算出的最终工具（不含 MCP）
    tools: list[str]
    # 勾选的公共 MCP server（不归工具权限管；另有它自己的私有 server）
    mcp_servers: list[str]
    # 全部角色与全部工具的权限 id，写 roles / allow / deny 时参照
    available_roles: list[RoleDetail]
    permissions: list[ToolPermission]


def agents_toolset(
    agents: AgentService,
    roles: RoleService,
    lifecycle: AgentLifecycle,
) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def get_agent(agent: str) -> AgentDetail:
        """Show an agent's configuration, the tools it ends up with, its public MCP servers, every role and every
        tool permission id.
        Use your own name to see yours.

        Args:
            agent: Agent name.
        """
        target = await agents.find_by_name(agent)
        resolved = agents.resolve(await agents.rules(target))
        return AgentDetail(
            name=target.name,
            description=target.description,
            model=target.model,
            thinking=target.thinking,
            roles=list(target.roles),
            allow=list(target.allow),
            deny=list(target.deny),
            tools=resolved.tools,
            mcp_servers=list(target.mcp_servers),
            available_roles=[
                RoleDetail(
                    name=r.name, description=r.description, allow=list(r.rules.allow), deny=list(r.rules.deny)
                )
                for r in await roles.list()
            ],
            permissions=[ToolPermission.of(t, TOOL_PERMISSIONS[t]) for t in agents.tools],
        )

    @ts.tool
    async def create_agent(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        model: str | None = None,
        roles: list[str] | None = None,
        allow: tuple[str, ...] = (),
        deny: tuple[str, ...] = (),
        mcp_servers: tuple[str, ...] = (),
        thinking: Thinking = "default",
    ) -> str:
        """Create a new long-lived agent with its own memory, skills and workspace, e.g. a specialist you will
        delegate to repeatedly. For a one-off job delegate to yourself instead (`ask_agent` / `delegate_task`
        with your own name).

        Args:
            name: Display name, unique among agents.
            description: What it is for (its role); it is shown to the agent itself and other agents choose
                whom to delegate to by it. The agent starts with an empty memory and grows it by itself.
            model: `<provider>:<model>`; defaults to the server's default model.
            roles: Its roles (`get_agent` lists every role and permission id); defaults to `agent` (every tool
                except writing what all agents share and managing agents).
            allow: Extra permission patterns to allow, e.g. `issue:*` (fnmatch wildcards).
            deny: Permission patterns to deny; deny wins over every allow, e.g. `trigger:*`.
            mcp_servers: Public MCP servers whose tools it gets (besides its own private ones).
            thinking: Thinking/reasoning effort of its model: `default` sends nothing (the model's own default),
                `off` disables it, the others set the effort level.
        """
        spec = AgentSpec(
            name=name,
            description=description,
            model=model,
            roles=roles,
            allow=allow,
            deny=deny,
            mcp_servers=mcp_servers,
            thinking=thinking,
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
        roles: list[str] | None = None,
        allow: tuple[str, ...] | None = None,
        deny: tuple[str, ...] | None = None,
        mcp_servers: tuple[str, ...] | None = None,
        thinking: Thinking | None = None,
    ) -> str:
        """Change an agent's configuration, including your own (use your own name). Only the given fields change.

        Args:
            agent: Name of the agent to change.
            new_name: New display name.
            description: New description.
            model: New `<provider>:<model>`.
            roles: New full list of roles.
            allow: New full list of extra permission patterns to allow.
            deny: New full list of permission patterns to deny.
            mcp_servers: New full list of public MCP servers whose tools it gets.
            thinking: New thinking/reasoning effort (`default` = the model's own default).
        """
        target = await agents.find_by_name(agent)
        patch = AgentPatch(
            name=new_name,
            description=description,
            model=model,
            roles=roles,
            allow=allow,
            deny=deny,
            mcp_servers=mcp_servers,
            thinking=thinking,
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
