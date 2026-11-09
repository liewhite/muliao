"""分发子域的规则，只在这里写一次：

能做某个动作（Grants.can，动作是 use 或 write，agents.permissions.Action）：agent 是资源的 owner（隐含的专属角色，use 与
write 都有），或它的规则（base + 角色 + 自己的 allow / deny）放行这个权限（Rules.allows）。deny 只拦规则分来的，不作用于
自己的资源。用户（界面）全部能做。admin 能改任何资源靠它固定的 `write:*`，不另写特判。owner 为 None 的资源归平台
（platform/ 目录下的，以及内置工具）。改不了时 Forbidden，并说出 owner 是谁（check_change）。

由此派生的也都在这里：一份配置能用哪些（reach_of）、资源分发出去了没有与收录它的角色（sharing_of），以及在快照上加上规则的
changeable / writable / usable。全部按一份资源快照（agents.assets.Snapshot）算，资源的 owner 就是它所在的目录。

规则按读出时角色的定义算：运行开始时算一次工具列表（Toolbox.profile），资源在用到时（skill 索引、MCP 连接、secret
解析）按当时的快照算。
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from muliao.agents.agent_rules import AgentRules
from muliao.agents.assets import Asset, Snapshot
from muliao.agents.ids import USER
from muliao.agents.kinds import Kind, StoredKind, as_kind, label
from muliao.agents.permissions import BASE, Action, Role, Rules, ToolSpec, pattern_of, spec_of
from muliao.errors import Forbidden


@dataclass(frozen=True)
class Grants:
    """一个使用者能对哪些资产做什么。agent_id 为 None 是用户：全部能做。"""

    agent_id: str | None
    rules: Rules

    def can(self, action: Action, kind: Kind, name: str, owner: str | None) -> bool:
        if self.agent_id is None:
            return True
        return owner == self.agent_id or self.rules.allows(action, kind, name)

    def uses(self, kind: Kind, name: str, owner: str | None) -> bool:
        return self.can(Action.USE, kind, name, owner)

    def writes(self, kind: Kind, name: str, owner: str | None) -> bool:
        return self.can(Action.WRITE, kind, name, owner)


USER_GRANTS = Grants(None, Rules())


# 「这一项能不能用」：(种类, 名字, owner)；Grants.uses 与 granted_by 都是它
Keep = Callable[[Kind, str, str | None], bool]


def granted_by(rules: Rules) -> Keep:
    """只按规则、不看 owner 的「能用」（Grants.can 的规则那一半）：一个角色本身、还没建的 agent。与 Grants.uses 同签名。"""
    return lambda kind, name, _owner: rules.includes(kind, name)


def grants_of(agent_id: str, rules: AgentRules, known: Mapping[str, Role]) -> Grants:
    return Grants(agent_id, rules.effective(known))


@dataclass(frozen=True)
class Source:
    """一个 agent 为什么能用某项资产（Grants.can 按来源拆开看）：它名下的、放行它的角色（含 base）、它自己的 allow。"""

    own: bool
    roles: tuple[str, ...]
    allow: bool


@dataclass(frozen=True)
class Provenance:
    """只解释、不判断：能不能用由 Grants 判断，这里说明是哪些来源放行的。最终规则是各来源（roles.sources_of）的并集，即各
    allow 的并集加上全部 deny，所以「某个来源放行」＝它的 allow 匹配且最终的 deny 没拦。"""

    agent_id: str
    # (角色名, 它的 allow)，最后一项角色名为 None：它自己的 allow
    allows: tuple[tuple[str | None, tuple[str, ...]], ...]
    deny: tuple[str, ...]

    @classmethod
    def of(cls, agent_id: str, rules: AgentRules, known: Mapping[str, Role]) -> Provenance:
        sources = rules.sources(known)
        return cls(
            agent_id,
            tuple((n, r.allow) for n, r in sources),
            Rules.union(r for _, r in sources).deny,
        )

    def explain(self, kind: Kind, name: str, owner: str | None) -> Source:
        """use 的来源；Grants.uses 为假时三项都空（调用方核对两者一致）。"""
        granting = [n for n, allow in self.allows if Rules(allow, self.deny).includes(kind, name)]
        return Source(
            owner == self.agent_id,
            tuple(n for n in granting if n is not None),
            None in granting,
        )


def grants_for(snapshot: Snapshot, actor: str) -> Grants:
    """actor（agent 的 id，或 ids.USER 即用户）的 Grants，按快照里角色的定义算。"""
    if actor == USER:
        return USER_GRANTS
    return grants_of(actor, snapshot.rules_of(actor), snapshot.roles)


@dataclass(frozen=True)
class Sharing:
    """条目的分发情况，一次读出角色与全部 agent 的规则后算多项。"""

    roles: tuple[Role, ...]
    base: Rules
    # 每个 agent（包括正在删除的）的最终规则
    agents: tuple[tuple[str, Rules], ...]

    def shared(self, kind: Kind, name: str, owner: str | None) -> bool:
        """「分发出去了」：base 收录它（人人可用，也给以后新建的 agent），或某个不是它 owner 的 agent 的规则收录它（放行
        use：admin 的 `write:*` 不算分发）。只有一个定义：条目页据此说明谁能用（CatalogEntry.shared），删 agent 时据此把它
        名下分发出去的资产转给平台。没分给任何 agent 的自建角色收录它不算。"""
        return self.base.includes(kind, name) or any(
            r.includes(kind, name) for a, r in self.agents if a != owner
        )

    def including(self, kind: Kind, name: str) -> tuple[str, ...]:
        """收录它的角色（含内置的）。"""
        return tuple(r.name for r in self.roles if r.effective.includes(kind, name))


def sharing_of(snapshot: Snapshot) -> Sharing:
    known = snapshot.roles
    agents = tuple((a, snapshot.rules_of(a).effective(known)) for a in snapshot.agents)
    return Sharing(tuple(known.values()), known[BASE].effective, agents)


def check_change(snapshot: Snapshot, actor: str, asset: Asset) -> None:
    """actor 能改这一项（Grants.can 的 write），否则 Forbidden。"""
    if not grants_for(snapshot, actor).writes(as_kind(asset.kind), asset.name, asset.owner):
        raise Forbidden(
            f"{label(asset.kind)} {asset.name!r} belongs to {snapshot.owner_label(asset.owner)}; only its owner, "
            f"an agent whose roles grant 'write:{asset.kind.value}:{asset.name}', or an admin can change it"
        )


def changeable(snapshot: Snapshot, kind: StoredKind, name: str, actor: str) -> Asset:
    """要改或删的已有资源：存在，且 actor 能改它（Grants.can 的 write）。"""
    asset = snapshot.asset(kind, name)
    check_change(snapshot, actor, asset)
    return asset


def writable(snapshot: Snapshot, kind: StoredKind, name: str, actor: str) -> Asset | None:
    """新建或整体改写之前：已存在就按Grants.can 的 write 检查并返回它（owner 不变）；不存在返回 None（磁盘上有同名但没加载的是 Conflict）。"""
    asset = snapshot.get(kind, name)
    if asset is None:
        snapshot.check_free(kind, name)
    else:
        check_change(snapshot, actor, asset)
    return asset


def usable(snapshot: Snapshot, kind: StoredKind, user: str) -> list[Asset]:
    """user（agent 的 id 或 ids.USER）能用的（Grants.can），按名称。"""
    return usable_by(snapshot, kind, grants_for(snapshot, user))


def usable_by(snapshot: Snapshot, kind: StoredKind, grants: Grants) -> list[Asset]:
    """同 usable，按已经算好的 grants。"""
    return [a for a in snapshot.of_kind(kind) if grants.uses(as_kind(kind), a.name, a.owner)]


@dataclass(frozen=True)
class Reach:
    """一份配置能用的：内置工具（按登记顺序）与存在 items 里的资产（`kind:name`，按种类、名称）。"""

    tools: list[ToolSpec]
    items: list[str]

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]


def reach_of(snapshot: Snapshot, available: Collection[str], rules: Rules, keep: Keep) -> Reach:
    """工具按 rules 在 available（当前配置下注册的工具）里取；资源是 keep(kind, name, owner) 为真的。界面预览、角色的覆盖范围、
    get_agent 共用。"""
    items = [
        pattern_of(as_kind(k), a.name)
        for k in sorted(StoredKind, key=lambda k: k.value)
        for a in snapshot.of_kind(k)
        if keep(as_kind(k), a.name, a.owner)
    ]
    return Reach([spec_of(n) for n in rules.tools(available)], items)
