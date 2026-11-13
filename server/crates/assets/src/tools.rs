//! 本领域的工具：列出能委派的 agent（list_agents）、看一个 agent 最终的配置与能用的（get_agent）、建与删 agent
//! （create_agent、delete_agent，管理类），保存 secret（vault_set）。改 agent 配置、角色与分发规则是改资源目录里的文件，
//! 不经工具。给模型看的是名称和描述，不出现 id。

use muliao_platform::ids::AgentId;
use std::sync::Arc;

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use muliao_platform::errors::DomainError;
use muliao_platform::tooling::{BuiltinTool, RunScope, ToolTraits, builtin};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::access::Actor;
use crate::distribution::Distribution;
use crate::formats::agent_file::Thinking;
use crate::lifecycle::AgentLifecycle;
use crate::rules::{AgentRules, Role};
use crate::secrets::Secrets;
use crate::service::{AgentService, AgentSpec, AgentView};
use crate::tree::Owner;

/// 本领域的全部工具（组合根交给运行端口的工具箱）。
pub fn tools(
    agents: Arc<AgentService>,
    distribution: Arc<Distribution>,
    lifecycle: Arc<AgentLifecycle>,
    secrets: Arc<Secrets>,
) -> Vec<BuiltinTool> {
    vec![
        builtin(ListAgents { agents: agents.clone() }, ToolTraits::read()),
        builtin(GetAgent { agents: agents.clone(), distribution }, ToolTraits::read()),
        builtin(CreateAgent { agents: agents.clone() }, ToolTraits::write().admin()),
        builtin(DeleteAgent { agents, lifecycle }, ToolTraits::write().admin()),
        builtin(VaultSet { secrets }, ToolTraits::write().secret_args(&["value"])),
    ]
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct AgentEntry {
    pub name: String,
    pub description: String,
}

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct AgentGroup {
    /// None：不属于任何分组的 agent
    pub group: Option<String>,
    pub agents: Vec<AgentEntry>,
}

/// list_agents 的结果：能委派的 agent（不含归档的、目录没加载的：都不能运行）按分组列出，不分组的在前，其余按组名；
/// 组内保持传入的顺序。
pub fn agent_directory(agents: &[AgentView]) -> Vec<AgentGroup> {
    let mut groups: Vec<AgentGroup> = Vec::new();
    for a in agents {
        let Some(config) = a.config.as_ref().filter(|_| !a.info.archived()) else { continue };
        let group = config.file.group().map(str::to_owned);
        let entry = AgentEntry { name: a.info.name.to_string(), description: config.file.description().to_owned() };
        match groups.iter_mut().find(|g| g.group == group) {
            Some(g) => g.agents.push(entry),
            None => groups.push(AgentGroup { group, agents: vec![entry] }),
        }
    }
    // None 排在 Some 之前，Some 之间按组名
    groups.sort_by(|a, b| a.group.cmp(&b.group));
    groups
}

#[derive(Deserialize, JsonSchema)]
pub struct NoArgs {}

pub struct ListAgents {
    agents: Arc<AgentService>,
}

impl Tool<RunScope> for ListAgents {
    type Args = NoArgs;

    fn name(&self) -> &str {
        "list_agents"
    }

    fn description(&self) -> &str {
        "List all agents and what each one is for. Returns a JSON array of groups, each with `group` (null = not \
         in any group) and `agents` (each with `name` and `description`). Use before delegating. Archived agents \
         are not listed: they cannot be used until the user unarchives them. Nor are agents whose agent.toml \
         cannot be loaded until it is fixed (one whose model cannot run is listed, but its runs fail)."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, _args: NoArgs) -> Result<ToolOutput, ToolError> {
        Ok(agenter::tool::json(&agent_directory(&self.agents.list_views().await?)))
    }
}

#[derive(Debug, Serialize)]
pub struct RoleDetail {
    name: String,
    description: String,
    /// 内置角色（base、admin）代码里固定的规则，不可改
    fixed_allow: Vec<String>,
    allow: Vec<String>,
    deny: Vec<String>,
}

impl RoleDetail {
    fn of(r: &Role) -> Self {
        Self {
            name: r.name.clone(),
            description: r.description.clone(),
            fixed_allow: r.fixed.allow.clone(),
            allow: r.rules.allow.clone(),
            deny: r.rules.deny.clone(),
        }
    }
}

#[derive(Debug, Serialize)]
pub struct AgentDetail {
    name: String,
    /// access.toml 里它那一节的节名（`[agents.<id>]`）
    id: AgentId,
    description: String,
    group: Option<String>,
    /// 已归档：不能对话、委派、指派，它的触发器不触发；用户在界面上取消
    archived: bool,
    model: String,
    thinking: Thinking,
    /// 角色（base 隐含，不列在这里）与它自己的 allow / deny
    roles: Vec<String>,
    allow: Vec<String>,
    deny: Vec<String>,
    /// 按规则最终能用的内置工具
    tools: Vec<String>,
    /// 能用（use）的 skill、MCP server、secret、记忆（`kind:name`）：自己的与规则放行的
    assets: Vec<String>,
    /// 全部角色与全部内置工具，写 roles / allow / deny 时参照
    available_roles: Vec<RoleDetail>,
    all_tools: Vec<String>,
}

#[derive(Deserialize, JsonSchema)]
pub struct AgentArgs {
    /// Agent name.
    agent: String,
}

pub struct GetAgent {
    agents: Arc<AgentService>,
    distribution: Arc<Distribution>,
}

impl Tool<RunScope> for GetAgent {
    type Args = AgentArgs;

    fn name(&self) -> &str {
        "get_agent"
    }

    fn description(&self) -> &str {
        "Show an agent's configuration, the built-in tools and the skills/MCP servers/secrets/memories it ends up \
         with, every role and every built-in tool. Use your own name to see yours. Returns a JSON object with \
         `name`, `id` (its section in access.toml is `[agents.<id>]`), `description`, `group`, `archived`, `model`, `thinking`, `roles` (besides `base`), `allow`, \
         `deny`, `tools` (the built-in tools it ends up with), `assets` (`kind:name` of the skills, MCP servers, \
         secrets and memory blocks it can use), `available_roles` (every role: `name`, `description`, \
         `fixed_allow`, `allow`, `deny`) and `all_tools` (every built-in tool)."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, args: AgentArgs) -> Result<ToolOutput, ToolError> {
        let view = self.agents.find_view(&args.agent).await?;
        let Some(config) = &view.config else {
            return Err(DomainError::not_found(format!(
                "agent {:?} has no loadable agent.toml (see the asset problems reported to it); it cannot run until \
                 that is fixed",
                view.info.name
            ))
            .into());
        };
        let reach = self.distribution.reach(&view.info.id).await?;
        let detail = AgentDetail {
            name: view.info.name.to_string(),
            id: view.info.id.clone(),
            description: config.file.description().to_owned(),
            group: config.file.group().map(str::to_owned),
            archived: view.info.archived(),
            model: config.file.model().to_owned(),
            thinking: config.file.thinking(),
            roles: view.rules.roles.clone(),
            allow: view.rules.allow.clone(),
            deny: view.rules.deny.clone(),
            tools: reach.tool_names().into_iter().map(str::to_owned).collect(),
            assets: reach.assets,
            available_roles: self.distribution.list().await?.iter().map(RoleDetail::of).collect(),
            all_tools: self.distribution.tool_names().await?,
        };
        Ok(agenter::tool::json(&detail))
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct CreateArgs {
    /// Display name, unique among agents.
    name: String,
    /// What it is for (its role); it is shown to the agent itself and other agents choose whom to delegate to by
    /// it. The agent starts with an empty memory and grows it by itself.
    description: String,
    /// `<provider>:<model>`; defaults to the server's default model.
    #[serde(default)]
    model: Option<String>,
    /// Its roles besides `base`, which every agent has (`get_agent` lists them all).
    #[serde(default)]
    roles: Vec<String>,
    /// Extra rules `action:kind:name` (action use or write; kind tool, skill, mcp, secret or memory; fnmatch
    /// wildcards), e.g. `use:skill:relay-host`, `use:secret:RELAY_*`, `*:memory:trading`.
    #[serde(default)]
    allow: Vec<String>,
    /// Rules to deny; deny wins over every allow, e.g. `use:tool:trigger_*`.
    #[serde(default)]
    deny: Vec<String>,
    /// Thinking/reasoning effort of its model: `default` sets none (the model's own default), `off` disables it,
    /// the others set the effort level.
    #[serde(default)]
    thinking: Thinking,
    /// Group it belongs to, e.g. a batch of agents doing the same kind of job; `list_agents` lists agents by group.
    #[serde(default)]
    group: Option<String>,
}

pub struct CreateAgent {
    agents: Arc<AgentService>,
}

impl Tool<RunScope> for CreateAgent {
    type Args = CreateArgs;

    fn name(&self) -> &str {
        "create_agent"
    }

    fn description(&self) -> &str {
        "Create a new long-lived agent with its own memory, skills and workspace, e.g. a specialist you will \
         delegate to repeatedly. For a one-off job delegate to yourself instead (`ask_agent` / `delegate_task` \
         with your own name)."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: CreateArgs) -> Result<ToolOutput, ToolError> {
        let spec = AgentSpec {
            name: a.name,
            description: a.description,
            model: a.model,
            thinking: a.thinking,
            group: a.group,
            rules: AgentRules { roles: a.roles, allow: a.allow, deny: a.deny },
        };
        let created = self.agents.create(spec, &Actor::Agent(ctx.deps.agent_id.clone())).await?;
        Ok(agenter::tool::text(format!("created agent {}", created.name)))
    }
}

pub struct DeleteAgent {
    agents: Arc<AgentService>,
    lifecycle: Arc<AgentLifecycle>,
}

#[derive(Deserialize, JsonSchema)]
pub struct DeleteArgs {
    /// Name of the agent to delete.
    agent: String,
}

impl Tool<RunScope> for DeleteAgent {
    type Args = DeleteArgs;

    fn name(&self) -> &str {
        "delete_agent"
    }

    fn description(&self) -> &str {
        "Delete another agent with its sessions, memory, the skills, secrets and MCP servers it owns (those a role \
         or another agent's allow still hands out pass to the platform instead), triggers and workspace. \
         Irreversible. Threads its conversations opened with other agents that the user can see are kept as \
         standalone sessions; threads it was working on for others are deleted, and any unfinished work in them is \
         reported back to its delegator as deleted. You cannot delete yourself."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: DeleteArgs) -> Result<ToolOutput, ToolError> {
        let target = self.agents.find_by_name(&a.agent).await?;
        let me = &ctx.deps.agent_id;
        if &target.id == me {
            return Err(DomainError::invalid(
                "you cannot delete yourself while you are running; ask the user to do it in the app",
            )
            .into());
        }
        self.lifecycle.delete(&target.id, &Actor::Agent(me.clone())).await?;
        Ok(agenter::tool::text(format!("deleted agent {}", target.name)))
    }
}

pub struct VaultSet {
    secrets: Arc<Secrets>,
}

#[derive(Deserialize, JsonSchema)]
pub struct VaultSetArgs {
    /// Identifier like `SLACK_TOKEN`.
    key: String,
    /// The secret value.
    value: String,
    /// What it is for.
    #[serde(default)]
    description: String,
    /// false keeps the key out of commands you write yourself (run_shell, trigger prechecks and streams): only
    /// skill scripts run with skill_run and MCP configs can use it. Use it for keys that must only be used through
    /// a script wrapping the operation. Omitted: a new secret allows shell, an existing one keeps its setting.
    #[serde(default)]
    allow_shell: Option<bool>,
}

impl Tool<RunScope> for VaultSet {
    type Args = VaultSetArgs;

    fn name(&self) -> &str {
        "vault_set"
    }

    fn description(&self) -> &str {
        "Store a secret the user gave you so it never has to appear in files, skills, memory or MCP configs: it is \
         encrypted into `secrets/<KEY>.toml` in your asset directory (a new key is yours; changing an existing one \
         needs you to be able to write it). Keys are unique. The value is redacted from the saved conversation. \
         Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])` / \
         `skill_run(secrets=[KEY])`. To see keys, list the `secrets/` directories; to delete one, remove its file."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: VaultSetArgs) -> Result<ToolOutput, ToolError> {
        let me = &ctx.deps.agent_id;
        let shell = self
            .secrets
            .set(&a.key, &a.value, &a.description, &Actor::Agent(me.clone()), &Owner::Agent(me.clone()), a.allow_shell)
            .await?;
        Ok(agenter::tool::text(format!("stored secret {} (allow_shell: {shell})", a.key)))
    }
}
