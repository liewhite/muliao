//! MCP server：一个文件（`mcp/<名>.json`，在 owner 的资源目录下，格式见 [`crate::formats::mcp`]）。agent 用的 server：它能
//! 用的（自己的与分发给它的）、已启用的。把配置连接成工具是运行端口的事：界面新增时的连接验证由调用方交进来，这里不连接
//! 任何 server、不解析 secret。

use std::path::Path;
use std::sync::Arc;

use crate::access::{Actor, grants_for, usable};
use crate::error::{Result, conflict};
use crate::formats::mcp::{McpConfig, parse, render};
use crate::kinds::StoredKind;
use crate::source::AssetSource;
use crate::tree::{Asset, Owner};

/// 一个已保存的 MCP server。env / headers / args / url 的值可以包含 `${vault:KEY}`，按用它的 agent 能用的 secret 解析。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McpServer {
    pub owner: Owner,
    pub name: String,
    pub config: McpConfig,
    pub enabled: bool,
}

pub fn server_of(a: &Asset) -> McpServer {
    let file = a.content.mcp();
    McpServer { owner: a.owner.clone(), name: a.name.clone(), config: file.config.clone(), enabled: file.enabled }
}

pub struct McpRegistry {
    source: Arc<AssetSource>,
}

impl McpRegistry {
    pub fn new(source: Arc<AssetSource>) -> Self {
        Self { source }
    }

    /// agent 这次运行要连接的 server：它能用的、已启用的，按名称。
    pub async fn index(&self, agent_id: &str) -> Result<Vec<McpServer>> {
        let snapshot = self.source.snapshot().await?;
        let grants = grants_for(&snapshot, &Actor::Agent(agent_id.to_owned()));
        Ok(usable(&snapshot, StoredKind::Mcp, &grants).into_iter().map(server_of).filter(|s| s.enabled).collect())
    }

    pub async fn get(&self, name: &str) -> Result<McpServer> {
        Ok(server_of(self.source.snapshot().await?.asset(StoredKind::Mcp, name)?))
    }

    /// 用户在界面上新增 server（启用的，归 owner）。probe 给出时先用它连接验证（连不上是它返回的错误，不写入），返回它
    /// 列出的工具名。连接之前先查一次名字（已有同名的不必白跑一次连接）；写入口按此刻的树再查。
    pub async fn add(
        &self,
        name: &str,
        config: McpConfig,
        owner: Owner,
        probe: Option<impl AsyncFnOnce(&McpServer) -> Result<Vec<String>>>,
    ) -> Result<(McpServer, Vec<String>)> {
        let server = McpServer { owner: owner.clone(), name: name.to_owned(), config, enabled: true };
        let path = {
            let snapshot = self.source.snapshot().await?;
            snapshot.check_free(StoredKind::Mcp, name)?;
            snapshot.path_of(StoredKind::Mcp, name, &owner)?
        };
        let tools = match probe {
            Some(probe) => probe(&server).await?,
            None => Vec::new(),
        };
        let new = |p: &Path| {
            if p.exists() {
                return Err(conflict(format!(
                    "MCP server {name:?} already exists; names are unique, choose another name"
                )));
            }
            Ok(())
        };
        self.source.write(&[(path, render(&server.config, true))], Some(&new)).await?;
        tracing::info!(owner = ?owner, mcp = name, tools = tools.len(), "mcp server added");
        Ok((server, tools))
    }

    pub async fn set_enabled(&self, name: &str, enabled: bool) -> Result<McpServer> {
        let server = self.get(name).await?;
        let path = self.source.snapshot().await?.asset(StoredKind::Mcp, name)?.path.clone();
        // 只改启用状态：期间配置被别处改过、删掉的不盖掉
        let unchanged = |p: &Path| {
            let same = p.is_file()
                && std::fs::read_to_string(p)
                    .ok()
                    .and_then(|t| parse(&t).ok())
                    .is_some_and(|f| f.config == server.config);
            if !same {
                return Err(conflict(format!("MCP server {name:?} was changed elsewhere meanwhile; reload and retry")));
            }
            Ok(())
        };
        self.source.write(&[(path, render(&server.config, enabled))], Some(&unchanged)).await?;
        Ok(McpServer { enabled, ..server })
    }
}
