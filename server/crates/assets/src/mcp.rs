//! MCP server：一个文件（`mcp/<名>.json`，在 owner 的资源目录下，格式见 [`crate::formats::mcp`]）。agent 用的 server：它能
//! 用的（自己的与分发给它的）、已启用的。把配置连接成工具是运行端口的事：界面新增时的连接验证由调用方交进来，这里不连接
//! 任何 server、不解析 secret。

use crate::tree::Expect;
use std::sync::Arc;

use crate::access::{Grants, usable};
use crate::formats::mcp::{McpConfig, McpFile, parse, render};
use crate::kinds::StoredKind;
use crate::source::AssetSource;
use crate::tree::{Asset, Owner, Snapshot};
use muliao_platform::errors::{Result, conflict};

/// 一个已保存的 MCP server。env / headers / args / url 的值可以包含 `${vault:KEY}`，按用它的 agent 能用的 secret 解析。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct McpServer {
    pub owner: Owner,
    pub name: String,
    pub config: McpConfig,
    pub enabled: bool,
}

fn server_of((a, file): (&Asset, &McpFile)) -> McpServer {
    McpServer { owner: a.owner.clone(), name: a.name.clone(), config: file.config.clone(), enabled: file.enabled }
}

/// grants 能用的、已启用的 server，按名称。
pub(crate) fn servers_of(snapshot: &Snapshot, grants: &Grants) -> Vec<McpServer> {
    usable::<McpFile>(snapshot, grants).into_iter().map(server_of).filter(|s| s.enabled).collect()
}

pub struct McpRegistry {
    source: Arc<AssetSource>,
}

impl McpRegistry {
    pub fn new(source: Arc<AssetSource>) -> Self {
        Self { source }
    }

    pub async fn get(&self, name: &str) -> Result<McpServer> {
        Ok(server_of(self.source.snapshot().await?.typed::<McpFile>(name)?))
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
        self.source.write(&[(path, render(&server.config, true), Expect::Absent)]).await?;
        tracing::info!(owner = ?owner, mcp = name, tools = tools.len(), "mcp server added");
        Ok((server, tools))
    }

    pub async fn set_enabled(&self, name: &str, enabled: bool) -> Result<McpServer> {
        let server = self.get(name).await?;
        let path = self.source.snapshot().await?.asset(StoredKind::Mcp, name)?.path.clone();
        // 只改启用状态：此刻的文件就是读到 server 时的样子，期间被别处改过、删掉的不盖掉
        let now = crate::formats::read(&path).map_err(conflict)?;
        if parse(&now).ok().is_none_or(|f| f.config != server.config) {
            return Err(conflict(format!("MCP server {name:?} was changed elsewhere meanwhile; reload and retry")));
        }
        self.source.write(&[(path, render(&server.config, enabled), Expect::text(Some(&now)))]).await?;
        Ok(McpServer { enabled, ..server })
    }
}
