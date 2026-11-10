"""agent 的查看、创建与删除：列出能委派的 agent（list_agents）、看一个 agent 最终的配置与能用的（get_agent）、建与删 agent。改 agent
配置、角色与分发规则是改资源目录里的文件（agents.assets）。给模型看的是名称和描述，不出现 id。"""

from collections.abc import Iterable

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.distribution import Distribution
from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.permissions import Role
from muliao.agents.service import AgentService, AgentSpec, AgentView, Thinking
from muliao.errors import InvalidInput, NotFound
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope


class RoleDetail(BaseModel):
    name: str
    description: str
    # 内置角色（base、admin）代码里固定的规则，不可改
    fixed_allow: list[str]
    allow: list[str]
    deny: list[str]

    @classmethod
    def of(cls, r: Role) -> "RoleDetail":
        return cls(
            name=r.name,
            description=r.description,
            fixed_allow=list(r.fixed.allow),
            allow=list(r.rules.allow),
            deny=list(r.rules.deny),
        )


class AgentDetail(BaseModel):
    name: str
    description: str
    # 分组名，null 为不分组
    group: str | None
    # 已归档：不能对话、委派、指派，它的触发器不触发；用户在界面上取消
    archived: bool
    model: str
    thinking: Thinking
    # 角色（base 隐含，不列在这里）与它自己的 allow / deny
    roles: list[str]
    allow: list[str]
    deny: list[str]
    # 按规则最终能用的内置工具
    tools: list[str]
    # 能用（use）的 skill、MCP server、secret、记忆（`kind:name`）：自己的与规则放行的
    assets: list[str]
    # 全部角色与全部内置工具，写 roles / allow / deny 时参照
    available_roles: list[RoleDetail]
    all_tools: list[str]


class AgentEntry(BaseModel):
    name: str
    description: str


class AgentGroup(BaseModel):
    # null：不属于任何分组的 agent
    group: str | None
    agents: list[AgentEntry]


def agent_directory(agents: Iterable[AgentView]) -> list[AgentGroup]:
    """list_agents 的结果：能委派的 agent（不含归档的、目录没加载的：都不能运行）按分组列出，不分组的在前，其余按组名；
    组内保持传入的顺序。"""
    groups: dict[str | None, list[AgentEntry]] = {}
    for a in agents:
        if not a.info.archived and a.config is not None:
            groups.setdefault(a.config.group, []).append(
                AgentEntry(name=a.info.name, description=a.config.description)
            )
    order = sorted(groups, key=lambda g: (g is not None, g or ""))
    return [AgentGroup(group=g, agents=groups[g]) for g in order]


def agents_toolset(
    agents: AgentService,
    distribution: Distribution,
    lifecycle: AgentLifecycle,
) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentGroup]:
        """List all agents and what each one is for. Returns a JSON array of groups, each with `group` (null = not
        in any group) and `agents` (each with `name` and `description`). Use before delegating. Archived agents
        are not listed: they cannot be used until the user unarchives them. Nor are agents whose agent.toml
        cannot be loaded until it is fixed (one whose model cannot run is listed, but its runs fail)."""
        return agent_directory(await agents.list_views())

    @ts.tool_plain
    async def get_agent(agent: str) -> AgentDetail:
        """Show an agent's configuration, the built-in tools and the skills/MCP servers/secrets/memories it ends up with,
        every role and every built-in tool. Use your own name to see yours. Returns a JSON object with `name`,
        `description`, `group`, `archived`, `model`, `thinking`, `roles` (besides `base`), `allow`, `deny`,
        `tools` (the built-in tools it ends up with), `assets` (`kind:name` of the skills, MCP servers, secrets
        and memory blocks it can use), `available_roles` (every role: `name`, `description`, `fixed_allow`, `allow`, `deny`) and
        `all_tools` (every built-in tool).

        Args:
            agent: Agent name.
        """
        view = await agents.find_view(agent)
        target, config, rules = view.info, view.config, view.rules
        if config is None:
            raise NotFound(
                f"agent {target.name!r} has no loadable agent.toml (see the asset problems reported to it); "
                "it cannot run until that is fixed"
            )
        reach = await distribution.reach(target.id)
        return AgentDetail(
            name=target.name,
            description=config.description,
            group=config.group,
            archived=target.archived,
            model=config.model,
            thinking=config.thinking,
            roles=list(rules.roles),
            allow=list(rules.allow),
            deny=list(rules.deny),
            tools=reach.tool_names,
            assets=reach.assets,
            available_roles=[RoleDetail.of(r) for r in await distribution.list()],
            all_tools=list(distribution.tools),
        )

    @ts.tool
    async def create_agent(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        model: str | None = None,
        roles: tuple[str, ...] = (),
        allow: tuple[str, ...] = (),
        deny: tuple[str, ...] = (),
        thinking: Thinking = "default",
        group: str | None = None,
    ) -> str:
        """Create a new long-lived agent with its own memory, skills and workspace, e.g. a specialist you will
        delegate to repeatedly. For a one-off job delegate to yourself instead (`ask_agent` / `delegate_task`
        with your own name).

        Args:
            name: Display name, unique among agents.
            description: What it is for (its role); it is shown to the agent itself and other agents choose
                whom to delegate to by it. The agent starts with an empty memory and grows it by itself.
            model: `<provider>:<model>`; defaults to the server's default model.
            roles: Its roles besides `base`, which every agent has (`get_agent` lists them all).
            allow: Extra rules `action:kind:name` (action use or write; kind tool, skill, mcp, secret or memory;
                fnmatch wildcards), e.g. `use:skill:relay-host`, `use:secret:RELAY_*`, `*:memory:trading`.
            deny: Rules to deny; deny wins over every allow, e.g. `use:tool:trigger_*`.
            thinking: Thinking/reasoning effort of its model: `default` sends nothing (the model's own default),
                `off` disables it, the others set the effort level.
            group: Group it belongs to, e.g. a batch of agents doing the same kind of job; `list_agents` lists
                agents by group.
        """
        spec = AgentSpec(
            name=name,
            description=description,
            model=model,
            roles=roles,
            allow=allow,
            deny=deny,
            thinking=thinking,
            group=group,
        )
        created = await agents.create(spec, actor=ctx.deps.agent_id)
        return f"created agent {created.name}"

    @ts.tool
    async def delete_agent(ctx: RunContext[RunScope], agent: str) -> str:
        """Delete another agent with its sessions, memory, the skills, secrets and MCP servers it owns (those
        a role or another agent's allow still hands out pass to the platform instead), triggers and workspace.
        Irreversible. Threads its conversations opened with other agents that the user
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
        await lifecycle.delete(target.id, actor=ctx.deps.agent_id)
        return f"deleted agent {target.name}"

    return ts
