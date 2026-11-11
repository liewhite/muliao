"""分发子域的规则，只在这里写一次：

能做某个动作（Grants.can，动作是 use 或 write，agents.permissions.Action）：agent 是资源的 owner（隐含的专属角色，use 与
write 都有），或它的规则（base + 角色 + 自己的 allow / deny）放行这个权限（Rules.allows；私有记忆没有规则里的名字，只有
不限种类的规则管得到）。deny 只拦规则分来的，不作用于自己的资源。用户（界面）全部能做。admin 能改任何资源靠它固定的
`write:*`，不另写特判。owner 为 None 的资源归平台（platform/ 目录下的，以及内置工具）。改不了时 Forbidden，并说出 owner 是谁
（check_change）。

由此派生的也都在这里：一份配置能用哪些（reach_of）、资源分发出去了没有与收录它的角色（sharing_of），以及在快照上加上规则的
writable / usable。全部按一份资源快照（agents.assets.Snapshot）算，资源的 owner 就是它所在的目录。

写沙箱的领域输入也在这里（write_scope，#499）：「能不能改某项资源」只由 Grants 换算成资源树里的可写路径，文件工具与命令
都由沙箱回答。

规则按读出时角色的定义算：运行开始时算一次工具列表（Toolbox.profile），资源在用到时（skill 索引、MCP 连接、secret
解析）按当时的快照算。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from muliao.agents.assets import ACCESS_FILE, PLATFORM, Asset, Snapshot
from muliao.agents.kinds import Kind, StoredKind, as_kind, label
from muliao.agents.naming import USER
from muliao.agents.permissions import (
    BASE,
    Action,
    AgentRules,
    Role,
    Rules,
    ToolSpec,
    pattern_of,
    permission_of,
)
from muliao.errors import Forbidden
from muliao.platform.fs import real


@dataclass(frozen=True)
class Grants:
    """一个使用者能对哪些资产做什么：它名下的都能做，别的看规则。agent_id 为 None 的没有自己的资产：只按规则判断的一个
    角色或还没建的 agent（Grants(None, rules)），以及用户（USER_GRANTS，规则放行一切）。"""

    agent_id: str | None
    rules: Rules

    def can(self, action: Action, asset: Asset) -> bool:
        return (self.agent_id is not None and asset.owner == self.agent_id) or self.rules.allows(
            action, as_kind(asset.kind), asset.rule_name
        )

    def uses(self, asset: Asset) -> bool:
        return self.can(Action.USE, asset)

    def writes(self, asset: Asset) -> bool:
        return self.can(Action.WRITE, asset)


# 用户（界面）全部能做
USER_GRANTS = Grants(None, Rules(allow=("*:*",)))


@dataclass(frozen=True)
class Source:
    """一个 agent 为什么能用某项资产（Grants.can 按来源拆开看）：它名下的、放行它的角色（含 base）、它自己的 allow。"""

    own: bool
    roles: tuple[str, ...]
    allow: bool


@dataclass(frozen=True)
class Provenance:
    """只解释、不判断：能不能用由 Grants 判断，这里说明是哪些来源放行的。最终规则是各来源（AgentRules.sources）的并集，即各
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

    def explain(self, asset: Asset) -> Source:
        """use 的来源；Grants.uses 为假时三项都空（调用方核对两者一致）。"""
        kind, name = as_kind(asset.kind), asset.rule_name
        granting = [n for n, allow in self.allows if Rules(allow, self.deny).includes(kind, name)]
        return Source(
            asset.owner == self.agent_id,
            tuple(n for n in granting if n is not None),
            None in granting,
        )


def grants_for(snapshot: Snapshot, actor: str) -> Grants:
    """actor（agent 的 id，或 ids.USER 即用户）的 Grants，按快照里角色的定义算。"""
    if actor == USER:
        return USER_GRANTS
    return Grants(actor, snapshot.rules_of(actor).effective(snapshot.roles))


@dataclass(frozen=True)
class Sharing:
    """资产的分发情况，一次读出角色与全部 agent 的规则后算多项。"""

    roles: tuple[Role, ...]
    base: Rules
    # 每个 agent（包括正在删除的）的最终规则
    agents: tuple[tuple[str, Rules], ...]

    def shared(self, kind: Kind, name: str | None, owner: str | None) -> bool:
        """「分发出去了」：base 收录它（人人可用，也给以后新建的 agent），或某个不是它 owner 的 agent 的规则收录它（放行
        use：admin 的 `write:*` 不算分发）。只有一个定义：资产页据此说明谁能用（CatalogAsset.shared），删 agent 时据此把它
        名下分发出去的资产转给平台。没分给任何 agent 的自建角色收录它不算。name 是它在规则里的名字（Asset.rule_name）：
        私有记忆（None）不算分发，它只属于它的 agent，删 agent 时随目录删掉（它在平台没有名字）。"""
        if name is None:
            return False
        return self.base.includes(kind, name) or any(
            r.includes(kind, name) for a, r in self.agents if a != owner
        )

    def including(self, kind: Kind, name: str | None) -> tuple[str, ...]:
        """收录它的角色（含内置的）；name 同 shared。"""
        return tuple(r.name for r in self.roles if r.effective.includes(kind, name))


def sharing_of(snapshot: Snapshot) -> Sharing:
    known = snapshot.roles
    agents = tuple((a, snapshot.rules_of(a).effective(known)) for a in snapshot.agents)
    return Sharing(tuple(known.values()), known[BASE].effective, agents)


def check_change(snapshot: Snapshot, actor: str, asset: Asset) -> None:
    """actor 能改这一项（Grants.can 的 write），否则 Forbidden。"""
    if not grants_for(snapshot, actor).writes(asset):
        raise Forbidden(
            f"{label(asset.kind)} {asset.name!r} belongs to {snapshot.owner_label(asset.owner)}; only its owner, "
            f"an agent whose roles grant 'write:{asset.kind.value}:{asset.name}', or an admin can change it"
        )


def writable(snapshot: Snapshot, kind: StoredKind, name: str, actor: str) -> Asset | None:
    """Snapshot.existing，已存在的再要 actor 能改它（check_change）。"""
    asset = snapshot.existing(kind, name)
    if asset is not None:
        check_change(snapshot, actor, asset)
    return asset


def usable(snapshot: Snapshot, kind: StoredKind, user: str) -> list[Asset]:
    """user（agent 的 id 或 ids.USER）能用的（Grants.can），按名称。"""
    return usable_by(snapshot, kind, grants_for(snapshot, user))


def usable_by(snapshot: Snapshot, kind: StoredKind, grants: Grants) -> list[Asset]:
    """同 usable，按已经算好的 grants。"""
    return [a for a in snapshot.of_kind(kind) if grants.uses(a)]


@dataclass(frozen=True)
class WriteScope:
    """agent 能写哪里的领域输入（workspace.writable 并进默认可写范围），全部由它的 Grants 换算。资源树（tree）整个只按
    规则与归属管：先整个收回，哪怕它落在默认可写的目录或 `write:path` 授权的目录里（排在它们之后），所以 access.toml 与
    platform/ 下别的文件只有 admin 能写，agent 改不了自己的权限；再放开 assets，最后收回 withheld。
    - admin（规则放行不限种类的写，`write:*`）：assets 是整棵树，withheld 是它的 deny 拦下的别人的资源（如
      `write:memory:global`）；
    - 别的 agent：assets 是自己的目录加上规则放行 write 的别人的资源（记忆、MCP、secret 是那个文件，skill 是它的目录），
      withheld 为空。
    自己目录里的资源都能写（deny 不作用于自己的资源）。granted / revoked：`write:path:<目录>` 的 allow 与 deny（Rules.paths）。"""

    tree: Path
    assets: tuple[Path, ...]
    withheld: tuple[Path, ...]
    granted: tuple[str, ...]
    revoked: tuple[str, ...]
    agent_id: str
    snapshot: Snapshot = field(repr=False, compare=False)

    @property
    def admin(self) -> bool:
        """agent 是 admin（Snapshot.administers）：另外还能读写平台的私有文件（配置、备份、推送私钥，#539）。"""
        return self.snapshot.administers(self.agent_id)

    def refusal(self, path: Path) -> str | None:
        """资源树里的 path 被拒时补的说明：是谁的、找谁授权；不在树里为 None（用通用的说明）。"""
        tree = real(self.tree)
        target = real(path)
        if target != tree and tree not in target.parents:
            return None
        snapshot = self.snapshot
        own = snapshot.own_dir(self.agent_id)
        mine = f"its own directory {own}" if own is not None else "its own directory"
        rest = (
            f"[If this was a write: {path} is in the AI asset tree, where an agent may write only {mine}, the "
            "assets its roles grant it 'write:<kind>:<name>' on, and an admin everything."
        )
        asset = _containing(snapshot, target)
        if asset is not None:
            whose = f"{rest} It belongs to {snapshot.owner_label(asset.owner)} ({label(asset.kind)} {asset.name!r})"
            if asset.rule_name is None:
                return f"{whose}: it is that agent's private memory, which only it (or an admin) changes.]"
            return (
                f"{whose}: ask its owner or an admin to change it, or ask the user or an admin to grant you "
                f"'{permission_of(Action.WRITE, as_kind(asset.kind), asset.rule_name)}' in "
                f"{snapshot.root / PLATFORM / ACCESS_FILE}.]"
            )
        parts = target.relative_to(tree).parts
        others = {n for a, n in snapshot.names.items() if a != self.agent_id}
        if parts and parts[0] in others:
            return f"{rest} {parts[0]}/ is agent {parts[0]!r}'s directory: ask that agent or an admin.]"
        return (
            f"{rest} The platform's files (roles and permissions in {PLATFORM}/{ACCESS_FILE}, global and "
            "shared memories, public skills) and the tree's root are changed only by an admin: ask the user or "
            "an agent with the admin role.]"
        )


def _containing(snapshot: Snapshot, target: Path) -> Asset | None:
    """target（真实路径）所在的那一项资源：就是它的文件，或在 skill 目录里。"""
    for kind in StoredKind:
        for a in snapshot.of_kind(kind):
            own = real(a.path)
            if target == own or own in target.parents:
                return a
    return None


def write_scope(snapshot: Snapshot, agent_id: str) -> WriteScope:
    """agent 现在能写哪里（WriteScope），只由它的 Grants 换算：「能写别人的某项资源」与 check_change 是同一个判断
    （Grants.can 的 write），`write:*` 换成整棵树（它还要写 access.toml、新建资源的位置与任何人的目录）。"""
    grants = grants_for(snapshot, agent_id)
    own = snapshot.own_dir(agent_id)
    others = [a for k in StoredKind for a in snapshot.of_kind(k) if a.owner != agent_id]
    if snapshot.administers(agent_id):
        assets: tuple[Path, ...] = (snapshot.root,)
        withheld = tuple(a.path for a in others if not grants.writes(a))
    else:
        assets = (*([own] if own is not None else []), *(a.path for a in others if grants.writes(a)))
        withheld = ()
    granted, revoked = grants.rules.paths()
    return WriteScope(snapshot.root, assets, withheld, granted, revoked, agent_id, snapshot)


@dataclass(frozen=True)
class Reach:
    """一份配置能用的：内置工具（按注册顺序）与资产（`kind:name`，按种类、名称）。"""

    tools: list[ToolSpec]
    assets: list[str]

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self.tools]


def reach_of(snapshot: Snapshot, grants: Grants) -> Reach:
    """grants 能用的：工具在快照的工具表（当前配置下注册的工具）里取，资产在快照里取。界面预览、角色的覆盖范围、get_agent 共用。"""
    assets = [
        pattern_of(as_kind(k), a.name)
        for k in sorted(StoredKind, key=lambda k: k.value)
        for a in snapshot.of_kind(k)
        if grants.uses(a)
    ]
    return Reach([t for t in snapshot.tools.specs if grants.rules.includes(Kind.TOOL, t.name)], assets)
