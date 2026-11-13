//! 把 MCP 配置（资源目录的 `mcp/<名>.json`）连成一次运行的工具，以及界面新增 server 时的连接验证。
//!
//! - 一个 server 出问题只影响它自己：引用的 secret 不在了或解不开、连不上、列不出工具，这次运行就没有它的工具，其余 server
//!   照常可用；原因记日志，并作为提示放进这次运行开头的运行上下文告诉模型。
//! - stdio server 的进程关进用它的 agent 的写沙箱：它的 command 是 agent 在自己目录里写的，不关的话等于平台替它在沙箱外跑
//!   任意命令。用户在界面上验证连接时关在 owner 的限制里；平台的（只有 admin 能写）由用户验证时不关。
//! - 连接、调用失败的信息先把代入的 secret 明文换成 `[secret:KEY]`（同 run_shell 的脱敏）。
//! - 运行中调用工具失败只是这次调用失败交还模型（agenter）；连接随这次运行的工具一起丢掉时断开，stdio 的整个进程组被杀。

use muliao_platform::ids::AgentId;
use std::collections::BTreeMap;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use agenter::mcp::{McpConnection, McpServer as Server, McpTransport as Transport, connect};
use futures::future::BoxFuture;
use muliao_assets::access::Actor;
use muliao_assets::formats::mcp::McpConfig;
use muliao_assets::mcp::McpServer;
use muliao_assets::secrets::Secrets;
use muliao_platform::errors::DomainError;
use muliao_platform::errors::Error;
use muliao_platform::sandbox::Confinement;
use muliao_platform::shell::redact;

/// 连上 server 并列出工具最多等多久
pub const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);

/// 一个 agent 的命令在什么限制下跑（agent id → 它的写沙箱）。由工作区领域拼装，组合根注入。
pub type Confine = Arc<dyn Fn(AgentId) -> BoxFuture<'static, Result<Confinement, Error>> + Send + Sync>;

/// 一次运行连上的 server，和没连上的那些要告诉模型的话。
pub struct Reachable {
    pub connections: Vec<McpConnection>,
    pub notices: Vec<String>,
}

pub struct McpConnector {
    secrets: Arc<Secrets>,
    confine: Confine,
}

/// 连一个 server 的失败：Skip 是这个 server 的问题（secret 不在或解不开、连不上、列不出工具），跳过它，原因里带着 server
/// 名、已脱敏；System 是别的失败（让整次运行失败）。
enum BuildError {
    Skip(String),
    System(Error),
}

impl McpConnector {
    pub fn new(secrets: Arc<Secrets>, confine: Confine) -> Self {
        Self { secrets, confine }
    }

    /// 按 user（用它的 agent，或界面上的用户）能用的 secret 代入配置，得到连接用的配置与这份明文（脱敏用同一份）。
    /// cwd：stdio server 进程的工作目录——运行时是这次运行的工作目录（server 写出的文件落在 agent 读得到的地方）；验证连接
    /// 时为 None（沿用服务进程的）。
    async fn build(
        &self,
        server: &McpServer,
        user: &Actor,
        cwd: Option<&Path>,
    ) -> Result<(Server, BTreeMap<String, String>), BuildError> {
        let (filled, secrets) = self.secrets.fill_mcp(user, &server.config).await.map_err(|e| match e.domain() {
            Some(d) => BuildError::Skip(format!("MCP server {:?}: {d}", server.name)),
            None => BuildError::System(e),
        })?;
        let transport = match filled {
            McpConfig::Stdio { command, args, env } => {
                let mut argv: Vec<String> = std::iter::once(command).chain(args).collect();
                let confined_as = match user {
                    Actor::Agent(id) => Some(id.clone()),
                    Actor::User => server.owner.agent().cloned(),
                };
                if let Some(agent) = confined_as {
                    argv = (self.confine)(agent).await.map_err(BuildError::System)?.wrap(&argv);
                }
                let mut argv = argv.into_iter();
                Transport::Stdio {
                    command: argv.next().expect("argv has the command"),
                    args: argv.collect(),
                    env: env.into_iter().collect(),
                    cwd: cwd.map(Path::to_path_buf),
                }
            }
            McpConfig::Http { url, headers } => Transport::Http { url, headers: headers.into_iter().collect() },
        };
        Ok((Server { name: server.name.clone(), transport }, secrets))
    }

    async fn connect_one(
        &self,
        server: &McpServer,
        user: &Actor,
        cwd: Option<&Path>,
    ) -> Result<McpConnection, BuildError> {
        let (config, secrets) = self.build(server, user, cwd).await?;
        let redactor: agenter::mcp::Redact = Arc::new(move |text: &str| redact(text, &secrets));
        connect(&config, redactor, CONNECT_TIMEOUT).await.map_err(|e| BuildError::Skip(e.to_string()))
    }

    /// 这次运行要连接的 server（[`muliao_assets::run_assets::RunAssets::mcp`]：它能用的、已启用的）都连一遍（同时连）。连不上的跳过并写进 notices；
    /// 系统出错（读不了资源目录、拼不出写沙箱）让这次运行失败。
    pub async fn connect_for(
        &self,
        agent_id: &AgentId,
        servers: &[McpServer],
        workspace: &Path,
    ) -> Result<Reachable, Error> {
        let user = Actor::Agent(agent_id.to_owned());
        let attempts =
            futures::future::join_all(servers.iter().map(|s| self.connect_one(s, &user, Some(workspace)))).await;
        let mut reachable = Reachable { connections: Vec::new(), notices: Vec::new() };
        for (server, attempt) in servers.iter().zip(attempts) {
            match attempt {
                Ok(c) => reachable.connections.push(c),
                Err(BuildError::Skip(reason)) => reachable.notices.push(skip(agent_id, &server.name, &reason)),
                Err(BuildError::System(e)) => return Err(e),
            }
        }
        Ok(reachable)
    }

    /// 实际连接 server 并列出工具名（不带前缀），secret 按 user（添加它的 agent，或用户）能用的解析。连不上是预期结果之一，
    /// 以领域错误交还调用方修正。
    pub async fn probe(&self, server: &McpServer, user: &Actor) -> Result<Vec<String>, Error> {
        let connection = match self.connect_one(server, user, None).await {
            Ok(c) => c,
            Err(BuildError::Skip(reason)) => {
                tracing::info!(mcp = %server.name, error = %reason, "mcp probe failed");
                return Err(DomainError::invalid(format!("could not connect: {reason}")).into());
            }
            Err(BuildError::System(e)) => return Err(e),
        };
        let prefix = format!("{}_", server.name);
        Ok(connection
            .tools::<()>()
            .iter()
            .map(|t| t.def().name.strip_prefix(&prefix).unwrap_or(&t.def().name).to_owned())
            .collect())
    }
}

/// 这次运行跳过一个 server：记日志，返回告诉模型的话。reason 已脱敏。
pub(crate) fn skip(agent: &AgentId, server: &str, reason: &str) -> String {
    tracing::warn!(%agent, mcp = server, error = reason, "mcp server unavailable");
    format!("An MCP server is unavailable this run, so its tools are missing: {reason}")
}
