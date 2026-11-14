//! 分发的规则，只在这里写一次：能做某个动作（[`Grants::can`]）= 是资源的 owner（隐含的专属角色，use 与 write 都有），
//! 或它的规则（base + 角色 + 自己的 allow / deny）放行这个权限（私有记忆没有规则里的名字，只有不限种类的规则管得到）。
//! deny 只拦规则分来的，不作用于自己的资源。用户（界面）全部能做。admin 能改任何资源靠它固定的 `write:*`，不另写特判。
//!
//! 由此派生的也都在这里：一份配置能用哪些（[`reach_of`]）、资源分发出去了没有与收录它的角色（[`Sharing`]），以及写沙箱
//! 的领域输入（[`write_scope`]，#499）：「能不能改某项资源」只由 Grants 换算成资源树里的可写路径。

use muliao_platform::ids::AgentId;
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use muliao_platform::fs::{is_within, real};

use crate::access_file::ACCESS_FILE;
use crate::kinds::{KindContent, StoredKind};
use crate::naming::{AgentName, PLATFORM};
use crate::roles::{Role, RoleName, RuleSource};
use crate::rules::{Action, Actions, Rule, Rules, Target, ToolSpec, pattern_of, permission_of, permits};
use crate::tree::{Asset, AssetKey, Owner, Snapshot, dir_owner};
use muliao_platform::errors::{Result, forbidden};

/// 做事的人：用户（界面），或某个 agent（id）。
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum Actor {
    User,
    Agent(AgentId),
}

impl Actor {
    /// 存进库与日志的形式：用户是 `user`，agent 是它的 id。
    pub fn as_str(&self) -> &str {
        match self {
            Actor::User => crate::naming::USER,
            Actor::Agent(id) => id.as_str(),
        }
    }
}

/// 一个使用者能对哪些资产做什么，以及为什么：它名下的都能做，别的看规则的各个来源。agent_id 为 None 的没有自己的资产：
/// 只按规则判断的一个角色或还没建的 agent，以及用户（规则放行一切）。能不能做与是谁放行的是同一个判断（[`Grants::source`]）。
#[derive(Debug, Clone)]
pub struct Grants {
    agent_id: Option<AgentId>,
    /// 规则的各个来源的 allow
    allows: Vec<(RuleSource, Vec<Rule>)>,
    /// 全部来源的并集（deny 作用于每个来源的 allow）
    rules: Rules,
}

impl Grants {
    /// sources：规则的各个来源（[`crate::roles::BoundRules::sources`] 的形状）。
    pub fn new(agent_id: Option<AgentId>, sources: Vec<(RuleSource, Rules)>) -> Self {
        let rules = Rules::union(sources.iter().map(|(_, r)| r));
        Self { agent_id, allows: sources.into_iter().map(|(n, r)| (n, r.allow)).collect(), rules }
    }

    pub fn user() -> Self {
        Self::new(None, vec![(RuleSource::Own, Rules::new(vec![Rule::Every(Actions::Any)], vec![]))])
    }

    /// 最终规则（全部来源的并集）。
    pub fn rules(&self) -> &Rules {
        &self.rules
    }

    /// 能对这项资产做这个动作的来源：它名下的、放行它的角色（含 base）、它自己的 allow；都没有就是不能做。最终规则是各来源的
    /// 并集，所以「某个来源放行」＝它的 allow 匹配且最终的 deny 没拦。
    pub fn source(&self, action: Action, asset: &Asset) -> Source {
        let target = asset.target();
        let granting: Vec<&RuleSource> = self
            .allows
            .iter()
            .filter(|(_, allow)| permits(allow, &self.rules.deny, action, target))
            .map(|(n, _)| n)
            .collect();
        Source {
            own: self.agent_id.as_ref().is_some_and(|id| asset.owned_by(id)),
            roles: granting
                .iter()
                .filter_map(|n| match n {
                    RuleSource::Role(r) => Some(r.clone()),
                    RuleSource::Own => None,
                })
                .collect(),
            allow: granting.iter().any(|n| matches!(n, RuleSource::Own)),
        }
    }

    pub fn can(&self, action: Action, asset: &Asset) -> bool {
        self.source(action, asset).any()
    }

    pub fn uses(&self, asset: &Asset) -> bool {
        self.can(Action::Use, asset)
    }

    pub fn writes(&self, asset: &Asset) -> bool {
        self.can(Action::Write, asset)
    }
}

/// actor 的 Grants，按快照里角色的定义算。
pub fn grants_for(snapshot: &Snapshot, actor: &Actor) -> Grants {
    match actor {
        Actor::User => Grants::user(),
        Actor::Agent(id) => Grants::new(Some(id.clone()), snapshot.rules_of(id).sources()),
    }
}

/// 一个 agent 为什么能做某件事：它名下的、放行它的角色（含 base）、它自己的 allow。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Source {
    pub own: bool,
    pub roles: Vec<RoleName>,
    pub allow: bool,
}

impl Source {
    pub fn any(&self) -> bool {
        self.own || !self.roles.is_empty() || self.allow
    }
}

/// 资产的分发情况，一次读出角色与全部 agent 的规则后算多项。
pub struct Sharing {
    roles: Vec<(RoleName, Rules)>,
    base: Rules,
    /// 每个加载了的 agent 的最终规则
    agents: Vec<(AgentId, Rules)>,
}

impl Sharing {
    pub fn of(snapshot: &Snapshot) -> Self {
        let known = &snapshot.roles;
        Self {
            roles: known.values().map(|r| (r.name.clone(), r.effective())).collect(),
            base: known.base().effective(),
            agents: snapshot.agents.keys().map(|a| (a.clone(), snapshot.rules_of(a).effective())).collect(),
        }
    }

    /// 「分发出去了」：base 收录它（人人可用），或某个不是它 owner 的 agent 的规则收录它（admin 的 `write:*` 不算分发）。
    /// 资产页据此说明谁能用，删 agent 时据此把它名下分发出去的资产转给平台。
    pub fn shared(&self, target: Target<'_>, owner: &Owner) -> bool {
        self.base.includes(target) || self.agents.iter().any(|(a, r)| !owner.is(a) && r.includes(target))
    }

    /// 收录它的角色（含内置的）。
    pub fn including(&self, target: Target<'_>) -> Vec<RoleName> {
        self.roles.iter().filter(|(_, r)| r.includes(target)).map(|(n, _)| n.clone()).collect()
    }
}

/// actor 能改这一项，否则 Forbidden 并说出 owner 是谁。
pub fn check_change(snapshot: &Snapshot, actor: &Actor, asset: &Asset) -> Result<()> {
    if grants_for(snapshot, actor).writes(asset) {
        return Ok(());
    }
    Err(forbidden(format!(
        "{} {:?} belongs to {}; only its owner, an agent whose roles grant 'write:{}:{}', or an admin can change it",
        asset.kind().label(),
        asset.name(),
        snapshot.owner_label(&asset.owner()),
        asset.kind(),
        asset.name()
    )))
}

/// 按名字认的这一种资源（[`Snapshot::existing`]）与它带类型的内容，已存在的再要 actor 能改它。
pub fn writable<'s, C: KindContent>(
    snapshot: &'s Snapshot,
    name: &str,
    actor: &Actor,
) -> Result<Option<(&'s Asset, &'s C)>> {
    if snapshot.existing(C::KIND, name)?.is_none() {
        return Ok(None);
    }
    let found = snapshot.typed::<C>(name)?;
    check_change(snapshot, actor, found.0)?;
    Ok(Some(found))
}

/// grants 能用的这一种资源与它们带类型的内容，按名称（记忆含各 agent 的私有记忆）。
pub fn usable<'s, C: KindContent>(snapshot: &'s Snapshot, grants: &Grants) -> Vec<(&'s Asset, &'s C)> {
    snapshot
        .of_kind(C::KIND)
        .into_iter()
        .filter(|a| grants.uses(a))
        .filter_map(|a| C::of(&a.content).map(|c| (a, c)))
        .collect()
}

/// agent 能写哪里的领域输入（工作区领域把它并进默认可写范围），全部由它的 Grants 换算。资源树（tree）整个只按规则与
/// 归属管：先整个收回，哪怕它落在默认可写的目录或 `write:path` 授权的目录里，所以 access.toml 与 platform/ 下别的文件只有
/// admin 能写，agent 改不了自己的权限；再放开 assets，最后收回 withheld。
/// - admin：assets 是整棵树，withheld 是它的 deny 拦下的别人的资源（如 `write:memory:global`）；
/// - 别的 agent：assets 是自己的目录加上规则放行 write 的别人的资源（记忆、MCP、secret 是那个文件，skill 是它的目录）。
///
/// withheld 只收回现在加载了的资源。自己目录里的资源都能写（deny 不作用于自己的资源）。granted / revoked：
/// `write:path:<目录>` 的 allow 与 deny。
#[derive(Debug, Clone)]
pub struct WriteScope {
    pub tree: PathBuf,
    pub assets: Vec<PathBuf>,
    pub withheld: Vec<PathBuf>,
    pub granted: Vec<String>,
    pub revoked: Vec<String>,
    /// agent 是 admin：另外还能读写平台的私有文件（配置、备份、推送私钥，#539）
    pub admin: bool,
    /// 资源树里被拒时补的说明要用的：它自己的目录、别的 agent（id → 名字）、加载了的资源
    own: Option<PathBuf>,
    others: BTreeMap<AgentId, AgentName>,
    located: Vec<(PathBuf, String)>,
}

impl WriteScope {
    /// 资源树里的 path 被拒时补的说明：是谁的、找谁授权；不在树里为 None（用通用的说明）。
    pub fn refusal(&self, path: &Path) -> Option<String> {
        let tree = real(&self.tree).ok()?;
        let target = real(path).ok()?;
        if !is_within(&tree, &target) {
            return None;
        }
        let mine = match &self.own {
            Some(own) => format!("its own directory {}", own.display()),
            None => "its own directory".into(),
        };
        let rest = format!(
            "[If this was a write: {} is in the AI asset tree, where an agent may write only {mine}, the assets its \
             roles grant it 'write:<kind>:<name>' on, and an admin everything.",
            path.display()
        );
        for (asset_path, whose) in &self.located {
            let Ok(own) = real(asset_path) else { continue };
            if is_within(&own, &target) {
                return Some(format!("{rest} {whose}"));
            }
        }
        let rel = target.strip_prefix(tree.as_path()).ok()?;
        if let Some(first) = rel.iter().next().map(|p| p.to_string_lossy().into_owned())
            && let Some(Owner::Agent(id)) = dir_owner(&self.others, &first)
        {
            let name = self.others[&id].as_str();
            return Some(format!("{rest} {first}/ is agent {name:?}'s directory: ask that agent or an admin.]"));
        }
        Some(format!(
            "{rest} The platform's files (roles and permissions in {PLATFORM}/{ACCESS_FILE}, global and shared memories, \
             public skills) and the tree's root are changed only by an admin: ask the user or an agent with the admin role.]"
        ))
    }
}

/// agent 现在能写哪里（[`WriteScope`]），只由它的 Grants 换算：「能写别人的某项资源」与 [`check_change`] 是同一个判断，
/// `write:*` 换成整棵树（它还要写 access.toml、新建资源的位置与任何人的目录）。
pub fn write_scope(snapshot: &Snapshot, agent_id: &AgentId) -> WriteScope {
    let grants = grants_for(snapshot, &Actor::Agent(agent_id.to_owned()));
    let own = snapshot.own_dir(agent_id);
    let others: Vec<&Asset> = snapshot.all().filter(|a| !a.owner().is(agent_id)).collect();
    let admin = snapshot.administers(agent_id);
    let (assets, withheld) = if admin {
        (vec![snapshot.root.clone()], others.iter().filter(|a| !grants.writes(a)).map(|a| a.path.clone()).collect())
    } else {
        let mut assets: Vec<PathBuf> = own.iter().cloned().collect();
        assets.extend(others.iter().filter(|a| grants.writes(a)).map(|a| a.path.clone()));
        (assets, Vec::new())
    };
    let (granted, revoked) = grants.rules().paths();
    let access_path = snapshot.access_path();
    let located = snapshot
        .all()
        .map(|a| {
            let whose =
                format!("It belongs to {} ({} {:?})", snapshot.owner_label(&a.owner()), a.kind().label(), a.name());
            let advice = match &a.key {
                AssetKey::Private { .. } => {
                    format!("{whose}: it is that agent's private memory, which only it (or an admin) changes.]")
                }
                AssetKey::Named { name, .. } => format!(
                    "{whose}: ask its owner or an admin to change it, or ask the user or an admin to grant you '{}' in \
                     {}.]",
                    permission_of(Action::Write, a.kind().kind(), name),
                    access_path.display()
                ),
            };
            (a.path.clone(), advice)
        })
        .collect();
    WriteScope {
        tree: snapshot.root.clone(),
        assets,
        withheld,
        granted,
        revoked,
        admin,
        own,
        others: snapshot.names.iter().filter(|(a, _)| *a != agent_id).map(|(a, n)| (a.clone(), n.clone())).collect(),
        located,
    }
}

/// 一份配置能用的：内置工具（按注册顺序）与资产（按种类、名称）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reach {
    pub tools: Vec<ToolSpec>,
    pub assets: Vec<UsableAsset>,
}

/// 能用的一项资产（存成文件的那四种）。
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, utoipa::ToSchema)]
pub struct UsableAsset {
    pub kind: StoredKind,
    pub name: String,
}

impl Reach {
    pub fn tool_names(&self) -> Vec<&str> {
        self.tools.iter().map(|t| t.name.as_str()).collect()
    }

    /// 资产写成 `kind:name`（给模型看的形式）。
    pub fn asset_patterns(&self) -> Vec<String> {
        self.assets.iter().map(|a| pattern_of(a.kind.kind(), &a.name)).collect()
    }
}

/// grants 能用的：工具在快照的工具表里取，资产在快照里取。界面预览、角色的覆盖范围、get_agent 共用。
pub fn reach_of(snapshot: &Snapshot, grants: &Grants) -> Reach {
    let assets = StoredKind::ALL
        .into_iter()
        .flat_map(|k| snapshot.of_kind(k))
        .filter(|a| grants.uses(a))
        .map(|a| UsableAsset { kind: a.kind(), name: a.name().to_owned() })
        .collect();
    let tools =
        snapshot.tools.specs().iter().filter(|t| grants.rules().includes(Target::tool(&t.name))).cloned().collect();
    Reach { tools, assets }
}

/// 带这个角色的 agent 的 id（base 每个 agent 都有）。
pub fn holders_of(snapshot: &Snapshot, role: &Role) -> Vec<AgentId> {
    snapshot.agents.keys().filter(|a| snapshot.rules_of(a).holds(&role.name)).cloned().collect()
}
