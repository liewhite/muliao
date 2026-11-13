//! AI 资源的读取入口（[`AssetSource`]）：把资源目录树与数据库里的 agent 身份对上，给出当前的快照。
//!
//! - 快照按树的指纹（每个文件的路径、修改时间、大小）与数据库里的 agent 缓存：文件没变就不重读，直接改文件下次读到就
//!   生效（运行开始时组装指令、算工具与分发、解析 secret，都读它）。
//! - agent 的配置（描述、模型、思考档位、分组）只在快照里，数据库只有身份。模型或思考档位在当前配置下不能运行的记成
//!   问题报给它；它的运行在取模型时当场失败，不沿用旧值。
//! - 改写一个资源文件（界面编辑记忆、SKILL.md、MCP，文件工具）只经写入口 [`AssetSource::write`]：在锁里按读树的规则
//!   校验再原子写入，界面与文件工具对同一份内容结论一致。
//! - 平台的其余写入（vault_set、建 / 改名 / 删 agent、改 access.toml、删除与转移）在 [`AssetSource::writing`] 里做：与读
//!   共用一把锁，读-改-写串行。
//! - 「资源文件变了」只由 [`AssetSource::watch`] 看文件系统得出（平台自己写的、agent 用 shell 改的、用户在别处改的都算）。
//!
//! 文件 IO 都很小（本机几百个小文件），除了读整棵树放到阻塞线程池，其余直接在 async 函数里做。

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use muliao_platform::db::Database;
use tokio::sync::{Mutex, MutexGuard};

use crate::asset_files;
use crate::error::Result;
use crate::formats::agent_file::{AGENT_FILE, AgentFile, Thinking};
use crate::kinds::StoredKind;
use crate::rules::{GLOBAL_MEMORY, ToolTable};
use crate::tree::{Expect, Owner, Problem, Snapshot, fingerprint, load_tree};

/// (模型, 思考档位) 在当前配置下能不能运行；不能时的原因（运行端口的模型注册表给）。
pub type ModelCheck = Arc<dyn Fn(&str, Thinking) -> std::result::Result<(), String> + Send + Sync>;

/// 写入口写成的一份文件。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Written {
    /// 实际存下的全文（记忆规整过空白）
    pub text: String,
    /// 写成时的修改时间
    pub modified: DateTime<Utc>,
}

#[derive(PartialEq, Eq)]
struct Key {
    tree: Vec<(PathBuf, i128, u64)>,
    known: BTreeMap<String, crate::naming::AgentName>,
    deleting: BTreeSet<String>,
}

#[derive(Default)]
struct State {
    key: Option<Key>,
    snapshot: Option<Arc<Snapshot>>,
}

pub struct AssetSource {
    db: Arc<Database>,
    root: PathBuf,
    memory_max_chars: usize,
    check_model: ModelCheck,
    tools: Arc<ToolTable>,
    /// 读（重新加载）与平台自己的读-改-写共用一把锁：写入期间不会读到写了一半的树
    state: Mutex<State>,
}

/// 平台自己改资源文件期间持有的锁（[`AssetSource::writing`]）：交出改之前的快照，释放时让缓存失效。
pub struct Writing<'a> {
    state: MutexGuard<'a, State>,
    pub snapshot: Arc<Snapshot>,
}

impl Drop for Writing<'_> {
    fn drop(&mut self) {
        self.state.key = None;
    }
}

impl AssetSource {
    pub fn new(
        db: Arc<Database>,
        root: PathBuf,
        memory_max_chars: usize,
        check_model: ModelCheck,
        tools: Arc<ToolTable>,
    ) -> Self {
        Self { db, root, memory_max_chars, check_model, tools, state: Mutex::new(State::default()) }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn memory_max_chars(&self) -> usize {
        self.memory_max_chars
    }

    pub(crate) fn db(&self) -> &Arc<Database> {
        &self.db
    }

    pub async fn snapshot(&self) -> Result<Arc<Snapshot>> {
        let mut state = self.state.lock().await;
        self.current(&mut state).await
    }

    async fn current(&self, state: &mut State) -> Result<Arc<Snapshot>> {
        let (known, deleting) = crate::agents::identities(&self.db).await?;
        let root = self.root.clone();
        let tree = tokio::task::spawn_blocking(move || fingerprint(&root)).await.expect("fingerprint panicked");
        let key = Key { tree, known, deleting };
        if let (Some(k), Some(s)) = (&state.key, &state.snapshot)
            && *k == key
        {
            return Ok(s.clone());
        }
        let (root, known, deleting, max, last, tools) = (
            self.root.clone(),
            key.known.clone(),
            key.deleting.clone(),
            self.memory_max_chars,
            state.snapshot.clone(),
            self.tools.clone(),
        );
        let snapshot =
            tokio::task::spawn_blocking(move || load_tree(&root, &known, &deleting, max, last.as_deref(), tools))
                .await
                .expect("loading the asset tree panicked");
        let snapshot = Arc::new(self.checked(snapshot));
        if !snapshot.problems.is_empty() {
            let problems: Vec<String> =
                snapshot.problems.iter().map(|p| format!("{}: {}", p.path.display(), p.reason)).collect();
            tracing::warn!(?problems, "asset tree has problems");
        }
        state.key = Some(key);
        state.snapshot = Some(snapshot.clone());
        Ok(snapshot)
    }

    /// 在当前配置下不能运行的模型与思考档位记成问题（agent 照样加载：界面能看能改，运行时报错）。
    fn checked(&self, snapshot: Snapshot) -> Snapshot {
        let problems: Vec<Problem> = snapshot
            .agents
            .values()
            .filter_map(|a| {
                (self.check_model)(a.file.model(), a.file.thinking()).err().map(|e| Problem {
                    path: a.path.join(AGENT_FILE),
                    reason: format!("{e}; the agent cannot run until this is fixed"),
                    agent: Some(a.id.clone()),
                })
            })
            .collect();
        snapshot.with_problems(problems)
    }

    /// path 是资源目录里的文件（按真实路径）。
    pub fn contains(&self, path: &Path) -> bool {
        asset_files::in_tree(&self.root, path)
    }

    /// agent.toml 的内容在当前配置下能运行（模型与思考档位），否则 InvalidInput。格式与字段的规则在 AgentFile。
    pub fn check_agent(&self, data: &AgentFile) -> Result<()> {
        (self.check_model)(data.model(), data.thinking()).map_err(crate::error::invalid)
    }

    /// 把资源目录里的这些文件写成给出的文本：改写资源文件的唯一入口。在锁里按此刻的树逐份校验（不合格是
    /// [`crate::error::AssetRejected`]），再核对每份此刻合不合调用方的预期（[`Expect`]：打开编辑时的版本、读到时的样子），
    /// 全部通过才逐个原子写入；有一份不过就一份都不写。返回每份实际存下的。
    pub async fn write(&self, texts: &[(PathBuf, String, Expect)]) -> Result<Vec<(PathBuf, Written)>> {
        let w = self.writing().await?;
        let check = |f: &AgentFile| (self.check_model)(f.model(), f.thinking());
        let mut stored = Vec::with_capacity(texts.len());
        for (path, text, _) in texts {
            stored.push((path, asset_files::vet(&w.snapshot, path, text, self.memory_max_chars, &check)?));
        }
        for (path, _, expect) in texts {
            expect.check(path)?;
        }
        let mut written = Vec::with_capacity(stored.len());
        for (path, text) in stored {
            muliao_platform::fs::write_atomic(path, text.as_bytes())?;
            // 写成时就取，仍在锁里：之后别处的写入不会被记成这次的
            let modified = crate::formats::modified(path)?;
            written.push((path.clone(), Written { text, modified }));
        }
        Ok(written)
    }

    /// 全局记忆归平台、base 固定收录、不能删：资源目录里没有它（全新安装）时建一个空的。启动时调用。
    pub async fn ensure_global_memory(&self) -> Result<()> {
        let w = self.writing().await?;
        let path = w.snapshot.path_of(StoredKind::Memory, GLOBAL_MEMORY, &Owner::Platform)?;
        if !path.exists() {
            muliao_platform::fs::write_atomic(&path, b"")?;
            tracing::info!(path = %path.display(), "empty global memory created");
        }
        Ok(())
    }

    /// 平台自己改资源文件：与读串行（读-改-写不会互相覆盖），交出改之前的快照，释放时让缓存失效。锁不可重入：持有
    /// 期间不要调 [`AssetSource::snapshot`]（会卡死），只用交出的快照。
    pub async fn writing(&self) -> Result<Writing<'_>> {
        let mut state = self.state.lock().await;
        let snapshot = self.current(&mut state).await?;
        Ok(Writing { state, snapshot })
    }

    /// 常驻：资源目录里的文件有改动（隐藏的不算）就调一次 changed，密集的改动合成一批（等 200ms 没有新改动）。返回
    /// 只在监听建不起来时（启动时报出）。
    pub async fn watch(&self, changed: impl Fn()) -> std::result::Result<(), notify::Error> {
        use notify::Watcher;
        // 文件系统报的是真实路径（如 macOS 的 /private/var）
        let root = muliao_platform::fs::real(&self.root).map_err(notify::Error::io)?.into_path_buf();
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let filter_root = root.clone();
        let mut watcher = notify::recommended_watcher(move |event: notify::Result<notify::Event>| match event {
            Ok(e) if e.paths.iter().any(|p| visible_change(&filter_root, p)) => {
                let _ = tx.send(());
            }
            Ok(_) => {}
            Err(e) => tracing::warn!(error = %e, "asset tree watcher error"),
        })?;
        watcher.watch(&root, notify::RecursiveMode::Recursive)?;
        while rx.recv().await.is_some() {
            while let Ok(Some(())) = tokio::time::timeout(Duration::from_millis(200), rx.recv()).await {}
            changed();
        }
        Ok(())
    }
}

/// 资源目录 root 下 path 的改动算不算资源文件变了：隐藏的文件与目录（及其中的）不算，同指纹；原子写入的临时文件也是
/// 隐藏的，rename 到的目标照常算。报来不在 root 下的照算（多通知一次无害）。
pub fn visible_change(root: &Path, path: &Path) -> bool {
    match path.strip_prefix(root) {
        Ok(rel) => !rel.iter().any(|p| p.to_string_lossy().starts_with('.')),
        Err(_) => true,
    }
}
