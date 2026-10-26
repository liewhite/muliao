"""agent 与角色的查看与管理：查看、创建、修改、删除 agent 与角色。给模型看的是名称和描述，不出现 id。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.permissions import Role
from muliao.agents.roles import RoleService, RoleSpec
from muliao.agents.service import AgentPatch, AgentService, AgentSpec, Thinking
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


def agents_toolset(
    agents: AgentService,
    roles: RoleService,
    lifecycle: AgentLifecycle,
) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool_plain
    async def get_agent(agent: str) -> AgentDetail:
        """Show an agent's configuration, the built-in tools and the skills/MCP servers/secrets it ends up with,
        every role and every built-in tool. Use your own name to see yours.

        Args:
            agent: Agent name.
        """
        target = await agents.find_by_name(agent)
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
            tools=agents.resolve(await agents.rules(target)),
            items=await agents.usable_items(target.id),
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
        target = await agents.update(target.id, patch)
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

    @ts.tool_plain
    async def create_role(
        name: str, description: str, allow: tuple[str, ...] = (), deny: tuple[str, ...] = ()
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
        role = await roles.create(RoleSpec(name, description, allow, deny))
        return f"created role {role.name}"

    @ts.tool_plain
    async def update_role(
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
        await roles.patch(name, description=description, allow=allow, deny=deny)
        return f"updated role {name}"

    @ts.tool_plain
    async def delete_role(name: str) -> str:
        """Delete a role no agent has. Built-in roles cannot be deleted.

        Args:
            name: Role name.
        """
        await roles.delete(name)
        return f"deleted role {name}"

    return ts
