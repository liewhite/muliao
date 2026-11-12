//! agent 目录（[`AgentService`]）：agent 的身份与配置及其工作目录。身份（id、名字）、归档、默认 agent、删除标记在 agents
//! 表（[`AgentInfo`]），名字也是它资源目录的目录名；描述、模型、思考档位、分组只在 `<名>/agent.toml`（快照的
//! [`Snapshot::loaded_agent`]）。新建与修改都先写文件（agent.toml、access.toml 里它的规则、空的私有记忆）再写表；改名就是
//! 挪目录。都在写资源目录的锁里做，任何一步失败把已改的文件改回去再报错。删除与归档的流程在 [`crate::lifecycle`]。
//!
//! 取消：文件是同步写的，中间没有 await；写表的事务一旦发出就在写线程上做完。所以调用方在写表时被取消，文件与行都已
//! 落下（一致）；只有「写表失败、调用方又恰好被取消」时撤回不会执行，留下一个 id 不认识的目录，读树时报出（罕见、可见）。

use std::path::{Path, PathBuf};
use std::sync::Arc;

use muliao_platform::db::Database;

use crate::access::Actor;
use crate::access_file::{self, AccessEdit};
use crate::agents::{self, AgentInfo, AgentNames};
use crate::error::{Result, conflict, invalid};
use crate::formats::agent_file::{self, AGENT_FILE, AgentConfig, AgentFile, Thinking};
use crate::naming::{new_agent_id, new_agent_name};
use crate::rules::AgentRules;
use crate::source::AssetSource;
use crate::tree::{AgentDir, PRIVATE_MEMORY_FILE, Snapshot, move_to, remove};

/// 新建 agent。model 为 None 时取服务端配置的默认模型。roles 不含 base（每个 agent 隐含）。
#[derive(Debug, Clone, Default)]
pub struct AgentSpec {
    pub name: String,
    pub description: String,
    pub model: Option<String>,
    pub thinking: Thinking,
    /// 分组名（去掉首尾空白，空串与 None 都是不分组）
    pub group: Option<String>,
    pub rules: AgentRules,
}

/// 修改 agent：为 None 的字段保持不变。归档不在这里：它要查 agent 是否空闲（[`crate::lifecycle`]）。
#[derive(Debug, Clone, Default)]
pub struct AgentPatch {
    pub name: Option<String>,
    pub config: AgentConfig,
    pub roles: Option<Vec<String>>,
    pub allow: Option<Vec<String>>,
    pub deny: Option<Vec<String>>,
}

/// 界面与 get_agent / list_agents 看到的 agent：身份、配置、分发规则。config 为 None：它的目录没加载（问题已报出），不能
/// 运行，但照样列出，以便修或删。
#[derive(Debug, Clone)]
pub struct AgentView {
    pub info: AgentInfo,
    pub config: Option<AgentDir>,
    pub rules: AgentRules,
}

fn view_of(snapshot: &Snapshot, info: AgentInfo) -> AgentView {
    let config = snapshot.agents.get(&info.id).cloned();
    let rules = snapshot.rules_of(&info.id);
    AgentView { info, config, rules }
}

pub struct AgentService {
    db: Arc<Database>,
    source: Arc<AssetSource>,
    workspaces_dir: PathBuf,
    default_model: String,
}

impl AgentService {
    pub fn new(source: Arc<AssetSource>, workspaces_dir: PathBuf, default_model: String) -> Self {
        Self { db: source.db().clone(), source, workspaces_dir, default_model }
    }

    pub fn workspace(&self, agent_id: &str) -> PathBuf {
        self.workspaces_dir.join(agent_id)
    }

    /// 启动时：数据库里已没有、还留着工作目录（删除时没删干净）的 agent 记录出来，由人决定是否清理（不阻止启动）。
    pub async fn report_leftovers(&self) -> Result<()> {
        let known = agents::identities(&self.db).await?.0;
        let Ok(entries) = std::fs::read_dir(&self.workspaces_dir) else { return Ok(()) };
        let mut leftovers: Vec<String> = entries
            .filter_map(|e| e.ok())
            .filter(|e| e.file_type().is_ok_and(|t| t.is_dir()))
            .map(|e| e.file_name().to_string_lossy().into_owned())
            .filter(|id| !known.contains_key(id))
            .collect();
        leftovers.sort();
        if !leftovers.is_empty() {
            tracing::warn!(agent_ids = ?leftovers, "workspaces of deleted agents remain");
        }
        Ok(())
    }

    /// actor：创建者。建资源目录（agent.toml、空的私有记忆）、写它的规则、建工作目录、插行。
    pub async fn create(&self, spec: AgentSpec, actor: &Actor) -> Result<AgentInfo> {
        let agent_id = new_agent_id();
        let name = new_agent_name(&spec.name)?;
        let group = spec.group.as_deref().map(str::trim).filter(|g| !g.is_empty()).map(str::to_owned);
        let file = AgentFile::new(
            agent_id.clone(),
            spec.description,
            spec.model.unwrap_or_else(|| self.default_model.clone()),
            spec.thinking,
            group,
        )
        .map_err(invalid)?;
        self.source.check_agent(&file)?;
        let w = self.source.writing().await?;
        let n = name.clone();
        self.db.write(move |tx| agents::check_name_free_in(tx, &n, None)).await?;
        check_directory_free(&w.snapshot, &name, None)?;
        // access.toml 的改动先算好（规则的校验都在这里）：它不合法时什么都不写
        let access = access_file::plan(&w.snapshot, |doc| access_file::set_agent_rules(doc, &name, &spec.rules))?;
        let directory = w.snapshot.root.join(&name);
        std::fs::create_dir_all(&w.snapshot.root)?;
        // 先单独建目录：已存在（绕开锁用 shell 建的）就在这里失败，下面的撤回只删自己建的
        std::fs::create_dir(&directory)?;
        let workspace = self.workspace(&agent_id);
        let done = async {
            write_new_agent(&directory, &file, &access)?;
            std::fs::create_dir_all(&workspace)?;
            let (id, n) = (agent_id.clone(), name.clone());
            self.db.write(move |tx| agents::insert_in(tx, &id, &n)).await
        }
        .await;
        match done {
            Ok(info) => {
                tracing::info!(agent = %agent_id, agent_name = %name, actor = actor.as_str(), "agent created");
                Ok(info)
            }
            Err(e) => {
                let removed = [remove(&directory), remove(&workspace)].into_iter().map(|r| r.map_err(Into::into));
                undo("creating an agent", std::iter::once(access.revert()).chain(removed));
                Err(e)
            }
        }
    }

    /// 只修改给出的字段：配置改 agent.toml（保留人手写的注释），规则改 access.toml，改名先挪目录、再改 access.toml 里那
    /// 一节，最后在同一把锁里写数据库（名字只认数据库）。agent 自己改描述、模型、思考档位是直接改它的 agent.toml。
    pub async fn update(&self, agent_id: &str, patch: AgentPatch) -> Result<AgentInfo> {
        let w = self.source.writing().await?;
        let id = agent_id.to_owned();
        let row = self.db.read(move |c| agents::active_agent_in(c, &id)).await?;
        let directory = w.snapshot.loaded_agent(agent_id)?.clone();
        let name = match &patch.name {
            Some(n) => new_agent_name(n)?,
            None => row.name.clone(),
        };
        let file = directory.file.with(&patch.config).map_err(invalid)?;
        self.source.check_agent(&file)?;
        let (id, n) = (agent_id.to_owned(), name.clone());
        self.db.write(move |tx| agents::check_name_free_in(tx, &n, Some(&id))).await?;
        check_directory_free(&w.snapshot, &name, Some(&directory.name))?;
        let before = w.snapshot.rules_of(agent_id);
        let rules = AgentRules {
            roles: patch.roles.unwrap_or(before.roles),
            allow: patch.allow.unwrap_or(before.allow),
            deny: patch.deny.unwrap_or(before.deny),
        };
        // 与建 agent 同：access.toml 的改动先算好，不合法时不动目录
        let access = access_file::plan(&w.snapshot, |doc| {
            if name != directory.name {
                access_file::rename_agent(doc, &directory.name, &name);
            }
            access_file::set_agent_rules(doc, &name, &rules)
        })?;
        let old_toml = std::fs::read_to_string(directory.path.join(AGENT_FILE))?;
        let target = directory.path.with_file_name(&name);
        let mut moved = false;
        let done = async {
            if name != directory.name {
                move_to(&directory.path, &target)?;
                moved = true;
            }
            write_agent_file(&target, &file)?;
            access.apply()?;
            let (id, n) = (agent_id.to_owned(), name.clone());
            self.db.write(move |tx| agents::rename_in(tx, &id, &n)).await
        }
        .await;
        if done.is_err() {
            let restore = || -> Result<()> {
                if moved {
                    std::fs::rename(&target, &directory.path)?;
                }
                muliao_platform::fs::write_atomic(&directory.path.join(AGENT_FILE), old_toml.as_bytes())?;
                Ok(())
            };
            undo("updating an agent", [access.revert(), restore()]);
        }
        done
    }

    /// 设为（或取消）系统默认 agent；设为默认时清掉旧的，始终最多一个。归档的不能设为默认。
    pub async fn set_default(&self, agent_id: &str, is_default: bool) -> Result<AgentInfo> {
        let id = agent_id.to_owned();
        self.db.write(move |tx| agents::set_default_in(tx, &id, is_default)).await
    }

    /// 还可用的 agent（正在删除的按不存在处理）。
    pub async fn get_active(&self, agent_id: &str) -> Result<AgentInfo> {
        let id = agent_id.to_owned();
        self.db.read(move |c| agents::active_agent_in(c, &id)).await
    }

    /// agent（包括正在删除的）。
    pub async fn get(&self, agent_id: &str) -> Result<AgentInfo> {
        let id = agent_id.to_owned();
        self.db.read(move |c| agents::agent_in(c, &id)).await
    }

    /// 模型按名称指代 agent（不接触 id）：在工具层把名称解析为 agent。
    pub async fn find_by_name(&self, name: &str) -> Result<AgentInfo> {
        let name = name.to_owned();
        self.db.read(move |c| agents::named_in(c, &name)).await
    }

    pub async fn names(&self) -> Result<AgentNames> {
        self.db.read(agents::agent_names_in).await
    }

    pub async fn list(&self) -> Result<Vec<AgentInfo>> {
        self.db.read(agents::listed_in).await
    }

    /// 还可用的 agent 加上它的配置与分发规则。
    pub async fn view(&self, agent_id: &str) -> Result<AgentView> {
        let snapshot = self.source.snapshot().await?;
        Ok(view_of(&snapshot, self.get_active(agent_id).await?))
    }

    /// 按名称找到的 agent 加上它的配置与分发规则（get_agent 工具）。
    pub async fn find_view(&self, name: &str) -> Result<AgentView> {
        let snapshot = self.source.snapshot().await?;
        Ok(view_of(&snapshot, self.find_by_name(name).await?))
    }

    pub async fn list_views(&self) -> Result<Vec<AgentView>> {
        let snapshot = self.source.snapshot().await?;
        Ok(self.list().await?.into_iter().map(|a| view_of(&snapshot, a)).collect())
    }

    /// 删除 agent 的工作目录（删除流程里）。删不干净（例如它留在后台的进程还在往里写）只记 warning：工作目录只是文件，
    /// 不能让删除卡住；残留由启动检查列出（[`AgentService::report_leftovers`]）。
    pub(crate) fn remove_workspace(&self, agent_id: &str) {
        let workspace = self.workspace(agent_id);
        if let Err(e) = remove(&workspace) {
            tracing::warn!(agent = agent_id, path = %workspace.display(), error = %e,
                "workspace of a deleted agent could not be fully removed");
        }
    }
}

/// 名字要没被资源目录里已有的目录占着（大小写不敏感的文件系统上只差大小写也算）；own 是这个 agent 现在的目录名。
fn check_directory_free(snapshot: &Snapshot, name: &str, own: Option<&str>) -> Result<()> {
    let fold = |s: &str| caseless::default_case_fold_str(s);
    let Ok(entries) = std::fs::read_dir(&snapshot.root) else { return Ok(()) };
    let taken = entries
        .filter_map(|e| e.ok())
        .map(|e| fold(&e.file_name().to_string_lossy()))
        .any(|n| n == fold(name) && own.is_none_or(|o| fold(o) != n));
    if taken {
        return Err(conflict(format!(
            "agent name {name:?} is already taken by a directory in {}",
            snapshot.root.display()
        )));
    }
    Ok(())
}

fn write_agent_file(directory: &Path, file: &AgentFile) -> Result<()> {
    let path = directory.join(AGENT_FILE);
    let current = if path.is_file() { Some(std::fs::read_to_string(&path)?) } else { None };
    let text = agent_file::render(current.as_deref(), file).map_err(invalid)?;
    muliao_platform::fs::write_atomic(&path, text.as_bytes())?;
    Ok(())
}

/// 在刚建的 agent 目录里写 agent.toml、空的私有记忆，再写入 access.toml 的改动。
fn write_new_agent(directory: &Path, file: &AgentFile, access: &AccessEdit) -> Result<()> {
    write_agent_file(directory, file)?;
    muliao_platform::fs::write_atomic(&directory.join(PRIVATE_MEMORY_FILE), b"")?;
    access.apply()
}

/// 撤回失败的写入：每一步都做，撤回自己出的错只记日志（原来的错误照常交回调用方）。
pub(crate) fn undo<E: std::fmt::Display>(what: &str, steps: impl IntoIterator<Item = std::result::Result<(), E>>) {
    for step in steps {
        if let Err(e) = step {
            tracing::error!(error = %e, "undoing a failed write while {what} also failed; check the asset tree");
        }
    }
}
