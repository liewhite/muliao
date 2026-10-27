"""agent 与角色的查看与管理：列出能委派的 agent（list_agents），查看、创建、修改、删除 agent 与角色，转移条目的 owner。给模型看的是名称和描述，不出现 id。"""

from collections.abc import Iterable

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.catalog import ItemCatalog
from muliao.agents.kinds import StoredKind, label
from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.permissions import Role
from muliao.agents.role_service import RoleService, RoleSpec
from muliao.agents.service import AgentInfo, AgentPatch, AgentService, AgentSpec, Thinking
from muliao.errors import Conflict, InvalidInput
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
    # 已归档：不能对话、委派、指派，它的触发器不触发；用 update_agent(archived=false) 取消
    archived: bool
    model: str
    thinking: Thinking
    # 角色（base 隐含，不列在这里）与它自己的 allow / deny
    roles: list[str]
    allow: list[str]
    deny: list[str]
    # 按规则最终能用的内置工具
    tools: list[str]
    # 能用的 skill、MCP server、secret（`kind:name`）：自己的与规则放行的
    items: list[str]
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


def agent_directory(agents: Iterable[AgentInfo]) -> list[AgentGroup]:
    """list_agents 的结果：能委派的 agent（不含归档的）按分组列出，不分组的在前，其余按组名；组内保持传入的顺序。"""
    groups: dict[str | None, list[AgentEntry]] = {}
    for a in agents:
        if not a.archived:
            groups.setdefault(a.group, []).append(AgentEntry(name=a.name, description=a.description))
    order = sorted(groups, key=lambda g: (g is not None, g or ""))
    return [AgentGroup(group=g, agents=groups[g]) for g in order]


def agents_toolset(
    agents: AgentService,
    roles: RoleService,
    lifecycle: AgentLifecycle,
    catalog: ItemCatalog,
) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def list_agents() -> list[AgentGroup]:
        """List all agents and what each one is for, by group (`group: null` = not in any group). Use before
        delegating. Archived agents are not listed: they cannot be used until unarchived (`update_agent`)."""
        return agent_directory(await agents.list())

    @ts.tool_plain
    async def get_agent(agent: str) -> AgentDetail:
        """Show an agent's configuration, the built-in tools and the skills/MCP servers/secrets it ends up with,
        every role and every built-in tool. Use your own name to see yours.

        Args:
            agent: Agent name.
        """
        target = await agents.find_by_name(agent)
        reach = await catalog.reach(target.id)
        return AgentDetail(
            name=target.name,
            description=target.description,
            group=target.group,
            archived=target.archived,
            model=target.model,
            thinking=target.thinking,
            roles=list(target.roles),
            allow=list(target.allow),
            deny=list(target.deny),
            tools=reach.tool_names,
            items=reach.items,
            available_roles=[RoleDetail.of(r) for r in await roles.list()],
            all_tools=list(agents.tools),
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
            allow: Extra rules `kind:name` (kind is tool, skill, mcp or secret; fnmatch wildcards), e.g.
                `skill:relay-host`, `secret:RELAY_*`.
            deny: Rules to deny; deny wins over every allow, e.g. `tool:trigger_*`.
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
        created = await agents.create(spec, author=ctx.deps.agent_id)
        return f"created agent {created.name}"

    @ts.tool
    async def update_agent(
        ctx: RunContext[RunScope],
        agent: str,
        new_name: str | None = None,
        description: str | None = None,
        model: str | None = None,
        roles: tuple[str, ...] | None = None,
        allow: tuple[str, ...] | None = None,
        deny: tuple[str, ...] | None = None,
        thinking: Thinking | None = None,
        group: str | None = None,
        archived: bool | None = None,
    ) -> str:
        """Change an agent's configuration, including your own (use your own name). Only the given fields change.
        An archived agent cannot be talked to, delegated to or assigned issues, and its triggers do not fire,
        until it is unarchived; archiving needs it to be idle.

        Args:
            agent: Name of the agent to change.
            new_name: New display name.
            description: New description.
            model: New `<provider>:<model>`.
            roles: New full list of roles (besides `base`, which every agent has).
            allow: New full list of extra `kind:name` rules to allow.
            deny: New full list of `kind:name` rules to deny.
            thinking: New thinking/reasoning effort (`default` = the model's own default).
            group: New group; an empty string removes it from its group.
            archived: `true` to archive a finished or inactive agent, `false` to unarchive it.
        """
        target = await agents.find_by_name(agent)
        patch = AgentPatch(
            name=new_name,
            description=description,
            model=model,
            roles=roles,
            allow=allow,
            deny=deny,
            thinking=thinking,
            group=group,
        )
        target = await agents.update(target.id, patch, author=ctx.deps.agent_id)
        if archived is not None:
            try:
                target = await lifecycle.set_archived(target.id, archived)
            except Conflict as e:
                if patch == AgentPatch():
                    raise
                # 归档要查空闲，与改配置不在一个事务里：说清楚哪部分生效了
                raise Conflict(f"{e} (the other changes were saved; only archiving failed)") from e
        state = " (archived)" if target.archived else ""
        return f"updated agent {target.name}{state}"

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
        await lifecycle.delete(target.id, author=ctx.deps.agent_id)
        return f"deleted agent {target.name}"

    @ts.tool
    async def transfer_item(ctx: RunContext[RunScope], kind: StoredKind, name: str, owner: str | None) -> str:
        """Give a skill, MCP server or secret to another owner, e.g. before deleting or archiving its current
        owner. Its content and the roles that hand it out stay; the new owner can always use and change it, the
        old one only through roles. Recorded in the item's version history.

        Args:
            kind: `skill`, `mcp` or `secret`.
            name: Item name.
            owner: Agent name of the new owner, or null for the platform (then only roles hand it out).
        """
        target = None if owner is None else await agents.find_by_name(owner)
        await catalog.transfer_item(kind, name, None if target is None else target.id, ctx.deps.agent_id)
        return f"{label(kind)} {name} now belongs to {'the platform' if target is None else f'agent {target.name}'}"

    @ts.tool
    async def create_role(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        allow: tuple[str, ...] = (),
        deny: tuple[str, ...] = (),
    ) -> str:
        """Create a role: a bundle of `kind:name` rules (kind is tool, skill, mcp or secret; fnmatch wildcards)
        that hands tools and items to every agent given the role (`update_agent` roles), e.g. a role
        `relay-trader` with allow `skill:relay-host`, `secret:RELAY_*`.

        Args:
            name: Lowercase slug.
            description: What the role is for.
            allow: Rules to allow.
            deny: Rules to deny; deny wins over every allow, also over other roles' allows.
        """
        role = await roles.create(RoleSpec(name, description, allow, deny), actor=ctx.deps.agent_id)
        return f"created role {role.name}"

    @ts.tool
    async def update_role(
        ctx: RunContext[RunScope],
        name: str,
        description: str | None = None,
        allow: tuple[str, ...] | None = None,
        deny: tuple[str, ...] | None = None,
    ) -> str:
        """Change a role; only the given fields change. Adding an item to a role (e.g. `skill:<name>` to the
        built-in `base` role every agent has) shares it with the agents having the role. The fixed rules of the
        built-in roles cannot change. Takes effect from those agents' next run.

        Args:
            name: Role name.
            description: New description.
            allow: New full list of rules to allow (`get_agent` shows the current ones).
            deny: New full list of rules to deny.
        """
        await roles.patch(name, actor=ctx.deps.agent_id, description=description, allow=allow, deny=deny)
        return f"updated role {name}"

    @ts.tool
    async def delete_role(ctx: RunContext[RunScope], name: str) -> str:
        """Delete a role no agent has. Built-in roles cannot be deleted.

        Args:
            name: Role name.
        """
        await roles.delete(name, actor=ctx.deps.agent_id)
        return f"deleted role {name}"

    return ts
