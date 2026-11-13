//! 删除与归档 agent（docs/architecture.md 第 10 节）。
//!
//! 删除（[`AgentLifecycle::delete`]）：
//! 1. 一个事务里把 agent 标为正在删除（不再出现在列表与名称解析里，各领域的写入口按不存在处理），并依次调各领域登记的
//!    清理（[`DeletionHook`]：会话领域删它的全部会话、有忙的就报 Conflict 整个回滚；issue 改为未指派；触发器删除……这里
//!    不列举是谁）。
//! 2. 提交后删文件：工作目录；它名下分发出去的资源（[`Sharing::shared`]）先挪到平台目录（别的 agent 还在用），其余随资源
//!    目录删掉；access.toml 里它的那一节；最后删这一行。文件不在事务里：失败时报错给调用方，agent 停在正在删除，服务启动
//!    时（[`AgentLifecycle::resume_deletions`]）接着删；每一步重做时已不在即空操作。没有任务队列（#675）。
//!
//! 归档（[`AgentLifecycle::set_archived`]）是可以来回切换的停用：只在它空闲（各领域登记的 [`ArchiveBlocker`] 都说不挡）、
//! 且不是默认 agent 时可做；之后给它派新工作的入口都拒绝（[`crate::agents::runnable_agent_in`]），数据都留着。

use std::path::PathBuf;
use std::sync::Arc;

use muliao_platform::db::{Database, Tx};
use rusqlite::Connection;

use crate::access::{Actor, Sharing};
use crate::access_file::{self, AccessEdit};
use crate::agents::{self, AgentInfo};
use crate::error::{Result, conflict};
use crate::source::AssetSource;
use crate::tree::{Owner, Snapshot, move_to, remove};

/// 删除 agent 时一个领域的清理：在标记它的事务里删掉或改掉自己引用它的数据；返回错误整个回滚（如它的会话忙）。
pub type DeletionHook = Arc<dyn Fn(&mut Tx<'_>, &str) -> Result<()> + Send + Sync>;

/// 归档会打断的工作还在进行（一个领域的只读查询，如它的会话忙）。
pub type ArchiveBlocker = Arc<dyn Fn(&Connection, &str) -> Result<bool> + Send + Sync>;

pub struct AgentLifecycle {
    db: Arc<Database>,
    source: Arc<AssetSource>,
    /// 各 agent 的工作目录所在（[`crate::service::workspace_of`]）
    workspaces_dir: PathBuf,
    hooks: Arc<Vec<DeletionHook>>,
    blockers: Arc<Vec<(String, ArchiveBlocker)>>,
}

impl AgentLifecycle {
    /// hooks、blockers：各领域登记的删除清理与归档前检查（组合根收集）。
    pub fn new(
        source: Arc<AssetSource>,
        workspaces_dir: PathBuf,
        hooks: Vec<DeletionHook>,
        blockers: Vec<(String, ArchiveBlocker)>,
    ) -> Self {
        Self { db: source.db().clone(), source, workspaces_dir, hooks: Arc::new(hooks), blockers: Arc::new(blockers) }
    }

    /// 发起删除并删完文件。标记之后删文件失败（或调用方中途被取消）时 agent 已不在列表里，停在正在删除，服务下次启动时
    /// 接着删。
    pub async fn delete(&self, agent_id: &str, actor: &Actor) -> Result<()> {
        let id = agent_id.to_owned();
        let agent = self.db.read(move |c| agents::deletable_in(c, &id)).await?;
        // 删除最后要改 access.toml：同样的改动现在就算一遍，不合法时先拒绝，不让 agent 停在正在删除、删文件一直失败
        retire_plan(&*self.source.snapshot().await?, &agent.id)?;
        let (id, by, hooks) = (agent_id.to_owned(), actor.as_str().to_owned(), self.hooks.clone());
        self.db
            .write(move |tx| {
                agents::mark_deleting_in(tx, &id, &by)?;
                for hook in hooks.iter() {
                    hook(tx, &id)?;
                }
                Ok::<_, crate::error::Error>(())
            })
            .await?;
        tracing::info!(agent = agent_id, actor = actor.as_str(), "agent deletion started");
        let workspace = crate::service::workspace_of(&self.workspaces_dir, agent_id);
        remove_files(self.db.clone(), self.source.clone(), workspace, agent_id.to_owned()).await
    }

    /// 启动时：接着删停在正在删除的 agent（上次删文件失败或服务中途重启）。失败的记 error，下次启动再试。
    pub async fn resume_deletions(&self) -> Result<()> {
        for (agent_id, _) in self.db.read(agents::deleting_in).await? {
            let workspace = crate::service::workspace_of(&self.workspaces_dir, &agent_id);
            if let Err(e) = remove_files(self.db.clone(), self.source.clone(), workspace, agent_id.clone()).await {
                tracing::error!(agent = %agent_id, error = %e, "removing the files of a deleted agent failed again");
            }
        }
        Ok(())
    }

    /// 归档（或取消归档）；已是目标状态时不做任何事。归档要求它不是系统默认 agent、且空闲（忙时 Conflict）。检查与标记
    /// 在同一个事务里。
    pub async fn set_archived(&self, agent_id: &str, archived: bool) -> Result<AgentInfo> {
        let (id, blockers) = (agent_id.to_owned(), self.blockers.clone());
        let (before, after) = self
            .db
            .write(move |tx| {
                let before = agents::active_agent_in(tx, &id)?;
                if archived && !before.archived() {
                    agents::check_archivable(&before)?;
                    let mut blocking = Vec::new();
                    for (name, blocked) in blockers.iter() {
                        if blocked(tx, &id)? {
                            blocking.push(name.clone());
                        }
                    }
                    if !blocking.is_empty() {
                        tracing::info!(agent = %id, blocked_by = ?blocking, "agent archiving refused: busy");
                        return Err(conflict(format!(
                            "agent {:?} is working right now; stop its sessions first, then archive",
                            before.name
                        )));
                    }
                }
                let after = agents::set_archived_in(tx, &id, archived)?;
                Ok((before, after))
            })
            .await?;
        if before.archived() != archived {
            tracing::info!(agent = agent_id, archived, "agent archive state changed");
        }
        Ok(after)
    }
}

/// 删工作目录、资源目录与 access.toml 里它的那一节，最后删这一行。行已不在时什么都不做；每一步重做时是空操作。
async fn remove_files(db: Arc<Database>, source: Arc<AssetSource>, workspace: PathBuf, agent_id: String) -> Result<()> {
    let id = agent_id.clone();
    // 只删标着正在删除的：行已不在（删除重做）时什么都不做
    if db.read(move |c| agents::being_deleted_in(c, &id)).await?.is_none() {
        return Ok(());
    }
    // 工作目录里可能有 venv、clone 的仓库：放到阻塞线程里删。删不干净（它留在后台的进程还在往里写）只记
    // warning：工作目录只是文件，不能让删除卡住；残留由启动检查列出（AgentService::report_leftovers）
    let path = workspace.clone();
    if let Err(e) = tokio::task::spawn_blocking(move || remove(&path)).await.expect("removing a workspace panicked") {
        tracing::warn!(agent = %agent_id, path = %workspace.display(), error = %e,
            "workspace of a deleted agent could not be fully removed");
    }
    {
        let w = source.writing().await?;
        retire_assets(&w.snapshot, &agent_id)?;
    }
    let id = agent_id.clone();
    if db.write(move |tx| agents::remove_in(tx, &id)).await? {
        tracing::info!(agent = %agent_id, "agent deleted");
    }
    Ok(())
}

/// 删掉 agent 的资源目录与 access.toml 里它的那一节；它名下分发出去的资源（base 收录，或别的 agent 的规则收录）先挪到
/// 平台目录，私有记忆不分发、随目录删掉。目录没加载成功（agent.toml 坏了等）时它名下没有加载出的资源可挪，照样按数据库
/// 里的名字删目录、按 id 删那一节，不留下占着这个名字的残留。
fn retire_assets(snapshot: &Snapshot, agent_id: &str) -> Result<()> {
    let directory = snapshot.own_dir(agent_id).expect("an agent being deleted is still in the database");
    if !snapshot.agents.contains_key(agent_id) && directory.exists() {
        // 没加载出的资源无从判断别人用不用，随目录删掉：记下路径，事后好从备份里找
        tracing::warn!(path = %directory.display(), "directory of a deleted agent removed without being loaded");
    }
    let sharing = Sharing::of(snapshot);
    let owner = Owner::Agent(agent_id.to_owned());
    for kind in crate::kinds::StoredKind::ALL {
        for a in snapshot.named(kind).filter(|a| a.owner == owner) {
            if sharing.shared(kind.kind(), Some(&a.name), &owner) {
                move_to(&a.path, &snapshot.path_of(kind, &a.name, &Owner::Platform)?)?;
                tracing::info!(kind = %kind, asset = %a.name, "asset moved to the platform with its agent deleted");
            }
        }
    }
    retire_plan(snapshot, agent_id)?.apply()?;
    remove(&directory)?;
    Ok(())
}

/// 去掉 access.toml 里这个 agent 那一节的改动（还没写）。
fn retire_plan(snapshot: &Snapshot, agent_id: &str) -> Result<AccessEdit> {
    access_file::plan(snapshot, |doc| {
        access_file::remove_agent(doc, agent_id);
        Ok(())
    })
}
