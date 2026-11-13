//! 分发的门面（[`Distribution`]）：角色的增删改与查看，agent 的规则与它最终能用的（reach），配置的预览。角色定义与各
//! agent 的规则都在 access.toml（写与预览都经 [`access_file::plan`]）；能用 / 能改的规则在 [`crate::access`]。

use std::sync::Arc;

use crate::access::{Actor, Grants, Reach, Source, WriteScope, grants_for, holders_of, reach_of, write_scope};
use crate::access_file;
use crate::error::{Result, conflict, invalid, not_found};
use crate::kinds::StoredKind;
use crate::mcp::{McpServer, server_of};
use crate::rules::{Action, AgentRules, BUILTIN_ROLES, Role, Rules};
use crate::source::AssetSource;
use crate::tree::{Owner, Snapshot};

#[derive(Debug, Clone)]
pub struct RoleSpec {
    pub name: String,
    pub description: String,
    pub rules: Rules,
}

/// 角色页的一项：角色、只按它的规则能用的、带它的 agent。
#[derive(Debug, Clone)]
pub struct RoleView {
    pub role: Role,
    pub reach: Reach,
    /// 带这个角色的 agent 的 id（base 是全部）
    pub holders: Vec<String>,
}

/// agent 能用的一项资源。
#[derive(Debug, Clone)]
pub struct UsedAsset {
    pub kind: StoredKind,
    pub name: String,
    pub owner: Owner,
    pub description: String,
    /// 为什么能用：它名下的 / 哪些角色 / 它自己的 allow
    pub source: Source,
    /// 能改
    pub editable: bool,
    /// MCP server 的配置与启用状态；别的种类为 None
    pub mcp: Option<McpServer>,
}

/// 预览还没建的 agent 或角色时，在 plan 里给它的那一节用的名字（只算、不写）
const PREVIEW_SECTION: &str = "preview";

fn role<'s>(snapshot: &'s Snapshot, name: &str) -> Result<&'s Role> {
    snapshot.roles.get(name).ok_or_else(|| not_found(format!("role {name:?} not found")))
}

fn role_view(snapshot: &Snapshot, role: &Role) -> RoleView {
    // reach 只按角色自己的规则，不含任何 agent 自己的资源
    RoleView {
        role: role.clone(),
        reach: reach_of(snapshot, &Grants::new(None, vec![(Some(role.name.clone()), role.effective())])),
        holders: holders_of(snapshot, role),
    }
}

pub struct Distribution {
    source: Arc<AssetSource>,
}

impl Distribution {
    pub fn new(source: Arc<AssetSource>) -> Self {
        Self { source }
    }

    async fn check_agent(&self, agent_id: &str) -> Result<()> {
        let id = agent_id.to_owned();
        self.source.db().read(move |c| crate::agents::active_agent_in(c, &id)).await.map(|_| ())
    }

    // ---- agent 的规则 ----

    /// agent 的最终规则（base + 角色 + 自己的 allow / deny），按角色现在的定义算：运行开放哪些工具。
    pub async fn rules(&self, agent_id: &str) -> Result<Rules> {
        Ok(grants_for(&*self.source.snapshot().await?, &Actor::Agent(agent_id.to_owned())).rules().clone())
    }

    /// agent 现在能写哪里（写沙箱用）：资源树按归属，加 `write:path` 授权。
    pub async fn write_scope(&self, agent_id: &str) -> Result<WriteScope> {
        Ok(write_scope(&*self.source.snapshot().await?, agent_id))
    }

    /// agent 最终能用的内置工具与资源：get_agent 与界面同一套计算。
    pub async fn reach(&self, agent_id: &str) -> Result<Reach> {
        let snapshot = self.source.snapshot().await?;
        Ok(reach_of(&snapshot, &grants_for(&snapshot, &Actor::Agent(agent_id.to_owned()))))
    }

    /// 全部内置工具（当前配置下注册的，按注册顺序）：get_agent 列出，写规则时参照。
    pub async fn tool_names(&self) -> Result<Vec<String>> {
        Ok(self.source.snapshot().await?.tools.names().map(str::to_owned).collect())
    }

    /// agent 能用的 skill、MCP server、secret、记忆（按种类、名称），每项带来源与能不能改：界面的 agent 详情。与运行时
    /// 同一套判断（能用就是有来源放行）。
    pub async fn assets(&self, agent_id: &str) -> Result<Vec<UsedAsset>> {
        self.check_agent(agent_id).await?;
        let snapshot = self.source.snapshot().await?;
        let grants = grants_for(&snapshot, &Actor::Agent(agent_id.to_owned()));
        let mut used = Vec::new();
        for kind in StoredKind::ALL {
            for a in snapshot.of_kind(kind) {
                let source = grants.source(Action::Use, a);
                if !source.any() {
                    continue;
                }
                used.push(UsedAsset {
                    kind,
                    name: a.name.clone(),
                    owner: a.owner.clone(),
                    description: a.description().to_owned(),
                    source,
                    editable: grants.writes(a),
                    mcp: (kind == StoredKind::Mcp).then(|| server_of(a)),
                });
            }
        }
        Ok(used)
    }

    /// agent（None 为还没建的）按还没保存的规则能用的内置工具与资源（界面预览），与写入时同样校验（只 plan、不写）。
    pub async fn preview_agent(&self, agent_id: Option<&str>, rules: &AgentRules) -> Result<Reach> {
        if let Some(id) = agent_id {
            self.check_agent(id).await?;
        }
        let snapshot = self.source.snapshot().await?;
        // 还没建的没有 id 与名字：节名与注释不影响校验
        let (section, name) = match agent_id.and_then(|id| snapshot.names.get_key_value(id)) {
            Some((id, name)) => (id.clone(), name.clone()),
            None => (crate::naming::new_agent_id(), crate::naming::AgentName::stored(PREVIEW_SECTION.to_owned())),
        };
        // plan 与下面的计算都从同一份快照出发：plan 认的角色快照里都有
        access_file::plan(&snapshot, |doc| access_file::set_agent_rules(doc, &section, &name, rules))?;
        // 还没建的 agent 没有自己的资源，只看规则
        let grants = Grants::new(agent_id.map(str::to_owned), rules.sources(&snapshot.roles));
        Ok(reach_of(&snapshot, &grants))
    }

    // ---- 角色 ----

    pub async fn list(&self) -> Result<Vec<Role>> {
        Ok(self.source.snapshot().await?.roles.values().cloned().collect())
    }

    pub async fn get(&self, name: &str) -> Result<Role> {
        Ok(role(&*self.source.snapshot().await?, name)?.clone())
    }

    pub async fn create(&self, spec: &RoleSpec) -> Result<Role> {
        {
            let w = self.source.writing().await?;
            if w.snapshot.roles.contains_key(&spec.name) {
                return Err(conflict(format!("role {:?} already exists", spec.name)));
            }
            write_role(&w.snapshot, spec)?;
        }
        self.get(&spec.name).await
    }

    /// 只改给出的字段（None 为不改），读与写串行：并发的局部修改互不覆盖。名称不改（agent 按名称引用它）；内置角色也
    /// 可以（固定的部分不变）。改动从带这个角色的 agent 的下一次运行起生效。
    pub async fn patch(
        &self,
        name: &str,
        description: Option<String>,
        allow: Option<Vec<String>>,
        deny: Option<Vec<String>>,
    ) -> Result<Role> {
        {
            let w = self.source.writing().await?;
            let current = role(&w.snapshot, name)?;
            let spec = RoleSpec {
                name: name.to_owned(),
                description: description.unwrap_or_else(|| current.description.clone()),
                rules: Rules::new(
                    allow.unwrap_or_else(|| current.rules.allow.clone()),
                    deny.unwrap_or_else(|| current.rules.deny.clone()),
                ),
            };
            write_role(&w.snapshot, &spec)?;
        }
        self.get(name).await
    }

    pub async fn delete(&self, name: &str) -> Result<()> {
        if BUILTIN_ROLES.contains(&name) {
            return Err(invalid(format!("role {name:?} is built in and cannot be deleted")));
        }
        let w = self.source.writing().await?;
        let holders = holders_of(&w.snapshot, role(&w.snapshot, name)?);
        if !holders.is_empty() {
            let names: Vec<&str> = holders.iter().map(|a| w.snapshot.agents[a].name.as_str()).collect();
            return Err(conflict(format!("role {name:?} is used by agents {names:?}")));
        }
        access_file::plan(&w.snapshot, |doc| {
            access_file::delete_role(doc, name);
            Ok(())
        })?
        .apply()
    }

    pub async fn views(&self) -> Result<Vec<RoleView>> {
        let snapshot = self.source.snapshot().await?;
        Ok(snapshot.roles.values().map(|r| role_view(&snapshot, r)).collect())
    }

    pub async fn view(&self, name: &str) -> Result<RoleView> {
        let snapshot = self.source.snapshot().await?;
        Ok(role_view(&snapshot, role(&snapshot, name)?))
    }

    /// 角色（None 为还没建的）按还没保存的规则能用的（界面预览），与写入时同样校验（只 plan、不写）；内置角色加上它固定
    /// 的部分。
    pub async fn preview_role(&self, name: Option<&str>, rules: &Rules) -> Result<Reach> {
        let snapshot = self.source.snapshot().await?;
        let current = name.map(|n| role(&snapshot, n)).transpose()?;
        // 还没建的没有名字与描述：都不影响规则的校验
        let spec = RoleSpec {
            name: current.map_or(PREVIEW_SECTION, |r| r.name.as_str()).to_owned(),
            description: current.map_or(PREVIEW_SECTION, |r| r.description.as_str()).to_owned(),
            rules: rules.clone(),
        };
        access_file::plan(&snapshot, |doc| access_file::set_role(doc, &spec.name, &spec.description, &spec.rules))?;
        let effective = match current {
            None => rules.clone(),
            Some(r) => Rules::union([&r.fixed, rules]),
        };
        Ok(reach_of(&snapshot, &Grants::new(None, vec![(None, effective)])))
    }
}

fn write_role(snapshot: &Snapshot, spec: &RoleSpec) -> Result<()> {
    access_file::plan(snapshot, |doc| access_file::set_role(doc, &spec.name, &spec.description, &spec.rules))?.apply()
}
