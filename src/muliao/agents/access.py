"""分发子域的规则（条目的两条，加上改 agent 配置的一条），只在这里写一次：

1. 能用（Grants.uses）：agent 能用的条目 = 它自己 owner 的 ∪ 它的规则（agents.permissions，base + 角色 + 自己的 allow /
   deny）放行的。deny 只作用于规则分来的，不作用于自己的条目。用户（界面）能用全部。
2. 能改（check_change_in）：用户、条目的 owner、带 admin 角色的 agent；否则 Forbidden，并说出 owner 是谁。owner 为 None
   的条目归平台（迁移前的公共条目、用户在界面上建的、内置工具）。
3. 改 agent 配置（Grants.check_agent_update）：用户、admin 能改任何 agent；别的 agent 只能改自己的描述、模型、思考强度。

由此派生的也都在这里：一份配置能用哪些（reach_in）、条目分发出去了没有与收录它的角色（sharing_in），以及条目子域的查询加上
规则的 changeable_in / writable_in / usable_in。

规则按读出时角色的定义算：运行开始时算一次工具列表（Toolbox.profile），条目在用到时（skill 索引、MCP 连接、secret
解析）按当时的定义算。
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents.active import check_active_in
from muliao.agents.agent_rules import AgentRules, agent_rules_in, all_agent_rules_in
from muliao.agents.ids import USER
from muliao.agents.items import check_owner_active_in, existing_in, item_in, items_in, keys_in, owner_label_in
from muliao.agents.kinds import Kind, StoredKind, as_kind, label
from muliao.agents.permissions import ADMIN, BASE, Role, Rules, ToolSpec, pattern_of, spec_of
from muliao.agents.roles import roles_in
from muliao.agents.tables import Item
from muliao.errors import Forbidden


@dataclass(frozen=True)
class Grants:
    """一个使用者能用什么、能改什么。agent_id 为 None 是用户：全部能用、全部能改。"""

    agent_id: str | None
    rules: Rules
    admin: bool

    @classmethod
    def of(cls, agent_id: str, roles: Collection[str], rules: Rules) -> Grants:
        """agent 的 Grants：rules 是它的最终规则（AgentRules.effective），带 admin 角色就能改任何条目。"""
        return cls(agent_id, rules, ADMIN in roles)

    def uses(self, kind: Kind, name: str, owner: str | None) -> bool:
        if self.agent_id is None:
            return True
        return owner == self.agent_id or self.rules.allows(kind, name)

    def changes(self, owner: str | None) -> bool:
        return self.agent_id is None or self.admin or owner == self.agent_id

    def check_agent_update(self, target: str, fields: Collection[str]) -> None:
        """规则 3：改 agent 的配置（fields 是要改的字段名，归档是 `archived`）。用户与 admin 能改任何 agent 的任何字段；
        别的 agent 只能改自己的 SELF_UPDATABLE（不能借此给自己加角色、规则，也不能改名、换组、归档），否则 Forbidden。"""
        if self.agent_id is None or self.admin:
            return
        if target != self.agent_id:
            raise Forbidden(
                "only an admin can change another agent; ask the user or an agent with the admin role"
            )
        refused = sorted(set(fields) - SELF_UPDATABLE)
        if refused:
            raise Forbidden(
                f"without the admin role you can only change your own {', '.join(sorted(SELF_UPDATABLE))}, "
                f"not {', '.join(refused)}; ask the user or an agent with the admin role"
            )


# 非 admin 的 agent 能改自己的哪些配置（#289）：角色、规则、名称、分组、归档只有用户与 admin 能改
SELF_UPDATABLE = frozenset({"description", "model", "thinking"})


USER_GRANTS = Grants(None, Rules(), admin=True)


# 「这一项能不能用」：(种类, 名字, owner)；Grants.uses 与 granted_by 都是它
Keep = Callable[[Kind, str, str | None], bool]


def granted_by(rules: Rules) -> Keep:
    """只按规则、不看 owner 的「能用」（规则 1 的规则那一半）：一个角色本身、还没建的 agent。与 Grants.uses 同签名。"""
    return lambda kind, name, _owner: rules.allows(kind, name)


def grants_of(agent_id: str, rules: AgentRules, known: dict[str, Role]) -> Grants:
    return Grants.of(agent_id, rules.roles, rules.effective(known))


async def grants_in(s: AsyncSession, actor: str) -> Grants:
    """actor（agent 的 id，或 ids.USER 即用户）的 Grants，按角色现在的定义算。agent 包括正在删除的（它的写入另由
    check_active_in 拦下）。"""
    if actor == USER:
        return USER_GRANTS
    return grants_of(actor, await agent_rules_in(s, actor), await roles_in(s))


@dataclass(frozen=True)
class Sharing:
    """条目的分发情况，一次读出角色与全部 agent 的规则后算多项。"""

    roles: tuple[Role, ...]
    base: Rules
    # 每个 agent（包括正在删除的）的最终规则
    agents: tuple[tuple[str, Rules], ...]

    def shared(self, kind: Kind, name: str, owner: str | None) -> bool:
        """「分发出去了」：base 收录它（人人可用，也给以后新建的 agent），或某个不是它 owner 的 agent 的规则放行它。只有一个
        定义：条目页据此说明谁能用（CatalogEntry.shared），删 agent 时据此把它名下分发出去的条目转给平台。没分给任何 agent
        的自建角色收录它不算。"""
        return self.base.allows(kind, name) or any(r.allows(kind, name) for a, r in self.agents if a != owner)

    def including(self, kind: Kind, name: str) -> tuple[str, ...]:
        """收录它的角色（含内置的）。"""
        return tuple(r.name for r in self.roles if r.effective.allows(kind, name))


async def sharing_in(s: AsyncSession) -> Sharing:
    known = await roles_in(s)
    agents = tuple((a, r.effective(known)) for a, r in (await all_agent_rules_in(s)).items())
    return Sharing(tuple(known.values()), known[BASE].effective, agents)


async def check_change_in(s: AsyncSession, actor: str, row: Item) -> None:
    """规则 2：actor 能改这一项，否则 Forbidden。"""
    if not (await grants_in(s, actor)).changes(row.owner):
        raise Forbidden(
            f"{label(StoredKind(row.kind))} {row.name!r} belongs to {await owner_label_in(s, row.owner)}; only its owner or an "
            "admin can change it"
        )


async def changeable_in(s: AsyncSession, kind: StoredKind, name: str, actor: str) -> Item:
    """要改或删的已有条目：existing_in，且 actor 能改它（规则 2）。"""
    row = await existing_in(s, kind, name)
    await check_change_in(s, actor, row)
    return row


async def writable_in(
    s: AsyncSession, kind: StoredKind, name: str, actor: str, owner: str | None
) -> Item | None:
    """新建或整体改写之前：已存在就按规则 2 检查并返回它（owner 不变）；不存在时返回 None，新条目将归 owner（agent 时
    检查它还可用）。"""
    row = await item_in(s, kind, name)
    if row is not None:
        await check_owner_active_in(s, kind, row)
        await check_change_in(s, actor, row)
        return row
    if owner is not None:
        await check_active_in(s, owner)
    return None


async def usable_in(s: AsyncSession, kind: StoredKind, user: str) -> list[Item]:
    """user（agent 的 id 或 ids.USER）能用的（规则 1），按名称。"""
    grants = await grants_in(s, user)
    return [r for r in await items_in(s, kind) if grants.uses(as_kind(kind), r.name, r.owner)]


@dataclass(frozen=True)
class Reach:
    """一份配置能用的：内置工具（按登记顺序）与 skill、MCP server、secret（`kind:name`，按种类、名称）。"""

    tools: list[ToolSpec]
    items: list[str]

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]


async def reach_in(s: AsyncSession, available: Collection[str], rules: Rules, keep: Keep) -> Reach:
    """工具按 rules 在 available（当前配置下注册的工具）里取；条目是 keep(kind, name, owner) 为真的。界面预览、角色的覆盖范围、
    get_agent 共用。"""
    items = [pattern_of(k, n) for k, n, o in await keys_in(s) if keep(k, n, o)]
    return Reach([spec_of(n) for n in rules.tools(available)], items)
