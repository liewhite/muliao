//! AI 资源的目录树（#498）：可编辑的 AI 资源都是文件，放在一棵目录树里，文件在谁的目录下就归谁。
//!
//! ```text
//! <root>/
//!   platform/                     归平台
//!     access.toml                 角色定义、各 agent 的角色与 allow / deny（crate::access_file）
//!     memory/<名>.md              全局记忆（global）与共享记忆
//!     skills/<名>/SKILL.md …      公共 skill
//!     mcp/<名>.json               MCP server
//!     secrets/<KEY>.toml          secret
//!   <agent 名>/                   归这个 agent
//!     agent.toml                  id、description、model、thinking、group
//!     memory.md                   私有记忆：按所属的 agent 认，不占记忆的名字（显示的名字是 agent 名）
//!     skills/<名>/  mcp/<名>.json  secrets/<KEY>.toml
//! ```
//!
//! 这里只管文件：布局（路径 ↔ 资源的双向函数 [`place_of`] / [`rel_of`]），把整棵树读成一份不可变的快照（[`load_tree`]，
//! 同步 IO；每种资源怎么读按它的 [`crate::kinds::KindSpec`]），以及平台自己的删除、挪动。读坏的文件不加载、记成
//! [`Problem`]。名字冲突的：其中一处在平台目录时只加载平台那份，其余各记一条（#502）；否则全部不加载、每处各记一条。
//! agent 目录按 agent.toml 里的 id 对应数据库里的 agent，目录名必须就是数据库里它的名字（名字只认数据库，改名只走平台）；
//! 对不上（id 不认识、重复、目录名不同）的整个目录不加载。隐藏文件与目录（`.` 开头）不读。

use muliao_platform::ids::AgentId;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::SystemTime;

use chrono::{DateTime, Utc};
use indexmap::IndexMap;
use sha2::{Digest, Sha256};

use crate::access_file::{self, ACCESS_FILE, Access, AccessText};
use crate::formats::agent_file::{self, AGENT_FILE, AgentFile};
use crate::kinds::{Content, Kind, KindContent, Section, StoredKind};
use crate::naming::{AgentName, PLATFORM};
use crate::rules::{Action, AgentRules, Roles, ToolTable};
use muliao_platform::errors::{Result, conflict, not_found};

pub const PRIVATE_MEMORY_FILE: &str = "memory.md";

// ---- 布局：owner 目录里的相对路径 ↔ 它是什么（读树、写前校验、平台新建与转移都只问这里） ----

/// owner 目录里的一个相对路径是什么。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Place {
    /// owner 的配置文件：平台的 access.toml，agent 的 agent.toml
    Config,
    /// 放一种资源的子目录（skills/ 等）
    Shelf(StoredKind),
    /// 一项按名字认的资源（平台记忆、MCP、secret 的文件，skill 的目录）；inner 是 skill 目录里的文件的相对路径
    Entry { kind: StoredKind, name: String, inner: Vec<String> },
    /// agent 目录里的私有记忆（memory.md）
    PrivateMemory,
}

/// 路径不是资源目录里该有的东西（kind 为 None），或名字不合那种资源的规则。
#[derive(Debug, Clone)]
pub struct Misplaced {
    pub reason: String,
    pub kind: Option<StoredKind>,
}

impl Misplaced {
    pub fn section(&self) -> Section {
        self.kind.map_or(Section::Layout, |k| k.spec().section)
    }
}

/// owner 目录里的相对路径 rel（非空）是什么（[`rel_of`] 的反向）：in_agent 是 agent 的目录（否则是平台目录）。
pub fn place_of(rel: &[String], in_agent: bool) -> Result<Place, Misplaced> {
    let (head, rest) = rel.split_first().expect("a relative path has at least one part");
    let config = if in_agent { AGENT_FILE } else { ACCESS_FILE };
    if rest.is_empty() && head == config {
        return Ok(Place::Config);
    }
    if in_agent && rest.is_empty() && head == PRIVATE_MEMORY_FILE {
        return Ok(Place::PrivateMemory);
    }
    let shelves: Vec<StoredKind> =
        StoredKind::ALL.into_iter().filter(|k| !in_agent || *k != StoredKind::Memory).collect();
    let joined = rel.join("/");
    let Some(kind) = shelves.iter().copied().find(|k| k.spec().shelf == head) else {
        let mut expected: Vec<&str> = shelves.iter().map(|k| k.spec().shelf).collect();
        expected.push(config);
        if in_agent {
            expected.push(PRIVATE_MEMORY_FILE);
        }
        expected.sort();
        return Err(Misplaced {
            reason: format!("{joined} is not a place for an asset file here (expected {expected:?})"),
            kind: None,
        });
    };
    let spec = kind.spec();
    let Some((entry, inner)) = rest.split_first() else { return Ok(Place::Shelf(kind)) };
    if !entry.ends_with(spec.suffix) || (!inner.is_empty() && !spec.is_directory) {
        let what =
            if spec.is_directory { format!("{} directories", spec.label) } else { format!("*{} files", spec.suffix) };
        return Err(Misplaced { reason: format!("{joined}: expected only {what} in {head}/"), kind: None });
    }
    let name = &entry[..entry.len() - spec.suffix.len()];
    spec.check_name(name).map_err(|reason| Misplaced { reason, kind: Some(kind) })?;
    Ok(Place::Entry { kind, name: name.to_owned(), inner: inner.to_vec() })
}

/// 一项按名字认的资源在 owner 目录里的相对路径（[`place_of`] 的反向）。名字不合这种资源的规则时报错。
pub fn rel_of(kind: StoredKind, name: &str) -> Result<PathBuf> {
    let spec = kind.spec();
    spec.check_name(name).map_err(muliao_platform::errors::invalid)?;
    Ok(Path::new(spec.shelf).join(format!("{name}{}", spec.suffix)))
}

// ---- 快照 ----

/// 资源归谁：平台，或某个 agent（id）。
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Owner {
    Platform,
    Agent(AgentId),
}

impl Owner {
    pub fn agent(&self) -> Option<&AgentId> {
        match self {
            Owner::Platform => None,
            Owner::Agent(id) => Some(id),
        }
    }

    pub fn is(&self, agent_id: &AgentId) -> bool {
        self.agent() == Some(agent_id)
    }
}

/// 一个加载成功的 agent 目录：目录名对上的数据库里的 agent（id 与名字），与它的 agent.toml（配置的唯一来源）。
#[derive(Debug, Clone)]
pub struct AgentDir {
    pub id: AgentId,
    pub name: AgentName,
    pub path: PathBuf,
    pub file: AgentFile,
}

/// 一项资源。path：记忆、MCP、secret 是那个文件，skill 是它的目录。私有记忆的 name 是它的 agent 的名字，只用于显示。种类就是内容的种类
/// （[`Asset::kind`]）。
#[derive(Debug, Clone)]
pub struct Asset {
    pub name: String,
    pub owner: Owner,
    pub path: PathBuf,
    pub updated_at: DateTime<Utc>,
    pub content: Content,
}

impl Asset {
    pub fn kind(&self) -> StoredKind {
        self.content.kind()
    }

    pub fn description(&self) -> &str {
        self.content.description()
    }

    /// 规则里指它的名字（`<action>:<kind>:<名>`）。私有记忆为 None：它按所属的 agent 认，只有不限种类的规则
    /// （`write:*`）管得到它。
    pub fn rule_name(&self) -> Option<&str> {
        match (self.kind(), &self.owner) {
            (StoredKind::Memory, Owner::Agent(_)) => None,
            _ => Some(&self.name),
        }
    }
}

/// 读树时发现的一处问题：哪个文件（或目录）、为什么没加载。agent 是它所在目录的 agent 的 id（平台目录与根目录为 None），
/// 据此报给相关的 agent。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Problem {
    pub path: PathBuf,
    pub reason: String,
    pub agent: Option<AgentId>,
}

/// 某一时刻整棵树的内容（只读）。
#[derive(Debug)]
pub struct Snapshot {
    pub root: PathBuf,
    /// 数据库里的 agent（id → 名字，含正在删除的）：目录读坏、没加载的 agent 也知道自己的目录该叫什么
    pub names: BTreeMap<AgentId, AgentName>,
    /// 正在删除的 agent 的 id：它们的目录照常读，但不再往里写（[`Snapshot::owner_dir`]、写入口）
    pub deleting: BTreeSet<AgentId>,
    /// 加载成功的 agent 目录，按 id（按目录名的顺序）
    pub agents: IndexMap<AgentId, AgentDir>,
    /// 每种按名字认的资源，按名字（记忆只有平台的）
    assets: BTreeMap<StoredKind, BTreeMap<String, Asset>>,
    /// 各 agent 的私有记忆，按 agent 的 id
    private: BTreeMap<AgentId, Asset>,
    pub roles: Roles,
    /// access.toml 里各 agent 的规则，按 id；没写的 agent 只有 base
    rules: BTreeMap<AgentId, AgentRules>,
    /// 读出 roles 与 rules 的 access.toml 原文（读坏时沿用上一份规则，这里记着读坏的原因）
    pub access_text: access_file::AccessText,
    /// 读这棵树时用的内置工具表
    pub tools: Arc<ToolTable>,
    pub problems: Vec<Problem>,
    /// 磁盘上有、但读坏了或名字冲突而没加载的资源名：新建同名的按已占用处理（[`Snapshot::check_free`]）
    unloaded: BTreeMap<StoredKind, BTreeSet<String>>,
    /// 被平台同名的那份遮住、没加载的 agent 目录里的副本（#502），按名字
    shadowed: BTreeMap<StoredKind, BTreeMap<String, Vec<PathBuf>>>,
}

impl Snapshot {
    pub fn rules_of(&self, agent_id: &AgentId) -> AgentRules {
        self.rules.get(agent_id).cloned().unwrap_or_default()
    }

    /// 按名字认的资源（私有记忆不在其中）。
    pub fn get(&self, kind: StoredKind, name: &str) -> Option<&Asset> {
        self.assets.get(&kind).and_then(|m| m.get(name))
    }

    /// 这一种按名字认的全部资源，按名字。
    pub fn named(&self, kind: StoredKind) -> impl Iterator<Item = &Asset> {
        self.assets.get(&kind).into_iter().flat_map(|m| m.values())
    }

    /// 新建之前：同名的已加载，或在磁盘上但没加载（读坏、冲突），都是 Conflict。
    pub fn check_free(&self, kind: StoredKind, name: &str) -> Result<()> {
        if self.get(kind, name).is_some() {
            return Err(conflict(format!(
                "{} {name:?} already exists; names are unique, choose another name",
                kind.label()
            )));
        }
        if self.unloaded.get(&kind).is_some_and(|s| s.contains(name)) {
            return Err(conflict(format!(
                "a {} named {name:?} is already on disk but not loaded (broken or duplicated; see the reported \
                 problems); fix or remove it first, or choose another name",
                kind.label()
            )));
        }
        Ok(())
    }

    /// 新建或整体改写之前：已存在就返回它（改写时 owner 不变）；不存在返回 None（同名在磁盘上但没加载的是 Conflict）。
    pub fn existing(&self, kind: StoredKind, name: &str) -> Result<Option<&Asset>> {
        match self.get(kind, name) {
            Some(a) => Ok(Some(a)),
            None => self.check_free(kind, name).map(|()| None),
        }
    }

    pub fn asset(&self, kind: StoredKind, name: &str) -> Result<&Asset> {
        self.get(kind, name).ok_or_else(|| not_found(format!("{} {name:?} not found", kind.label())))
    }

    /// 按名字认的这一种资源与它带类型的内容；没有时 NotFound。
    pub fn typed<C: KindContent>(&self, name: &str) -> Result<(&Asset, &C)> {
        let a = self.asset(C::KIND, name)?;
        C::of(&a.content).map(|c| (a, c)).ok_or_else(|| not_found(format!("{} {name:?} not found", C::KIND.label())))
    }

    /// agent 的私有记忆；它的目录里没有 memory.md（或读坏了）时 None。
    pub fn private_memory(&self, agent_id: &AgentId) -> Option<&Asset> {
        self.private.get(agent_id)
    }

    /// 这一种的全部资源（记忆含各 agent 的私有记忆），按名称。
    pub fn of_kind(&self, kind: StoredKind) -> Vec<&Asset> {
        let mut all: Vec<&Asset> = self.named(kind).collect();
        if kind == StoredKind::Memory {
            all.extend(self.private.values());
        }
        all.sort_by(|a, b| {
            (&a.name, a.owner.agent().map(AgentId::as_str).unwrap_or(""))
                .cmp(&(&b.name, b.owner.agent().map(AgentId::as_str).unwrap_or("")))
        });
        all
    }

    /// 全部资源（每种 [`Snapshot::of_kind`]）。
    pub fn all(&self) -> impl Iterator<Item = &Asset> {
        StoredKind::ALL.into_iter().flat_map(|k| self.of_kind(k))
    }

    /// agent 加载成功的目录（它的配置的唯一来源）。没加载（agent.toml 坏了、目录名对不上等）时 NotFound，带上报给它的
    /// 问题：这时它不能运行，要先修好文件。
    pub fn loaded_agent(&self, agent_id: &AgentId) -> Result<&AgentDir> {
        if let Some(a) = self.agents.get(agent_id) {
            return Ok(a);
        }
        let Some(name) = self.names.get(agent_id) else {
            return Err(not_found(format!("agent {agent_id:?} not found")));
        };
        let reasons: Vec<String> = self
            .problems
            .iter()
            .filter(|p| p.agent.as_ref() == Some(agent_id))
            .map(|p| format!("{}: {}", p.path.display(), p.reason))
            .collect();
        let detail = if reasons.is_empty() { String::new() } else { format!(" ({})", reasons.join("; ")) };
        Err(not_found(format!(
            "agent {name:?} has no loadable directory in {}{detail}; fix its agent.toml first",
            self.root.display()
        )))
    }

    /// owner 的目录：新写的东西放在这里。agent 正在删除、或没有加载成功的目录时 NotFound。
    pub fn owner_dir(&self, owner: &Owner) -> Result<PathBuf> {
        match owner {
            Owner::Platform => Ok(self.root.join(PLATFORM)),
            Owner::Agent(id) if self.deleting.contains(id) => Err(not_found(format!(
                "agent {:?} is being deleted; nothing can be written for it",
                self.names.get(id).map_or(id.as_str(), AgentName::as_str)
            ))),
            Owner::Agent(id) => Ok(self.loaded_agent(id)?.path.clone()),
        }
    }

    pub fn owner_label(&self, owner: &Owner) -> String {
        match owner {
            Owner::Platform => "the platform".into(),
            Owner::Agent(id) => format!("agent {:?}", self.agents.get(id).map_or(id.as_str(), |a| a.name.as_str())),
        }
    }

    /// owner 名下这一项按名字认的资源该在的位置（新建、转移时用）。
    pub fn path_of(&self, kind: StoredKind, name: &str, owner: &Owner) -> Result<PathBuf> {
        Ok(self.owner_dir(owner)?.join(rel_of(kind, name)?))
    }

    /// agent 的私有记忆该在的位置。
    pub fn private_memory_path(&self, agent_id: &AgentId) -> Result<PathBuf> {
        Ok(self.owner_dir(&Owner::Agent(agent_id.to_owned()))?.join(PRIVATE_MEMORY_FILE))
    }

    /// agent 的目录该在的位置（目录名就是数据库里的名字），不论它现在加载成功没有；不认识的 agent 为 None。
    pub fn own_dir(&self, agent_id: &AgentId) -> Option<PathBuf> {
        self.names.get(agent_id).map(|n| self.root.join(n))
    }

    /// admin：规则放行不限种类的写（`write:*`）。整棵树都能写；平台的问题报给它，平台的私有文件只有它能读写。
    pub fn administers(&self, agent_id: &AgentId) -> bool {
        self.rules_of(agent_id).effective(&self.roles).allows_every(Action::Write)
    }

    /// 报给这个 agent 的问题：它自己目录里的；平台的（platform/、根目录、access.toml）只报给 admin（免得几十个 agent
    /// 同时去改同一个平台文件）。别的照样记在服务日志里。
    pub fn problems_for(&self, agent_id: &AgentId) -> Vec<&Problem> {
        let admin = self.administers(agent_id);
        self.problems.iter().filter(|p| p.agent.as_ref() == Some(agent_id) || (p.agent.is_none() && admin)).collect()
    }

    pub fn access_path(&self) -> PathBuf {
        self.root.join(PLATFORM).join(ACCESS_FILE)
    }

    /// 规则能按名字指的全部 (种类, 名字)：注册的内置工具与按名字认的资源（不含私有记忆）。写规则时新加的至少要匹配其中
    /// 一项（[`access_file::vet`]）。
    pub fn addressable(&self) -> Vec<(Kind, String)> {
        self.tools
            .names()
            .map(|n| (Kind::Tool, n.to_owned()))
            .chain(self.assets.iter().flat_map(|(k, m)| m.keys().map(|n| (k.kind(), n.clone()))))
            .collect()
    }

    /// 被平台同名的那份遮住的 agent 目录里的副本。
    pub fn shadowed(&self, kind: StoredKind, name: &str) -> &[PathBuf] {
        self.shadowed.get(&kind).and_then(|m| m.get(name)).map_or(&[], Vec::as_slice)
    }

    /// 同一棵树、多几条问题（运行前检查出的，如模型在当前配置下不能用）。
    pub(crate) fn with_problems(mut self, extra: Vec<Problem>) -> Self {
        self.problems.extend(extra);
        self
    }
}

fn visible(dir: &Path) -> std::io::Result<Vec<PathBuf>> {
    let mut out: Vec<PathBuf> = std::fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| !p.file_name().is_some_and(|n| n.to_string_lossy().starts_with('.')))
        .collect();
    out.sort_by(|a, b| a.file_name().cmp(&b.file_name()));
    Ok(out)
}

fn file_name(path: &Path) -> String {
    path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// 读一棵树：一次性的状态（收集到的资源与问题）。
struct Reader<'a> {
    root: &'a Path,
    known: &'a BTreeMap<AgentId, AgentName>,
    memory_max_chars: usize,
    problems: Vec<Problem>,
    /// 每种资源：名字 → 找到的各处（冲突时不止一处）
    found: BTreeMap<StoredKind, BTreeMap<String, Vec<Asset>>>,
    unloaded: BTreeMap<StoredKind, BTreeSet<String>>,
    /// 平台目录里读坏了的名字：仍归平台占着，agent 的副本照样被遮住（不让副本趁平台那份坏了接管）
    broken_platform: BTreeMap<StoredKind, BTreeSet<String>>,
    shadowed: BTreeMap<StoredKind, BTreeMap<String, Vec<PathBuf>>>,
    private: BTreeMap<AgentId, Asset>,
}

impl Reader<'_> {
    fn problem(&mut self, path: PathBuf, reason: impl Into<String>, agent: Option<&AgentId>) {
        self.problems.push(Problem { path, reason: reason.into(), agent: agent.cloned() });
    }

    fn read_root(&mut self) -> IndexMap<AgentId, AgentDir> {
        let mut agents = IndexMap::new();
        if !self.root.is_dir() {
            return agents;
        }
        let entries = match visible(self.root) {
            Ok(e) => e,
            Err(e) => {
                self.problem(self.root.to_path_buf(), format!("cannot list: {e}"), None);
                return agents;
            }
        };
        let known = self.known;
        let by_name: BTreeMap<&AgentName, &AgentId> = known.iter().map(|(id, name)| (name, id)).collect();
        for entry in entries {
            if file_name(&entry) == PLATFORM {
                continue;
            }
            if !entry.is_dir() {
                self.problem(entry, "unexpected file; the root holds only platform/ and agent directories", None);
                continue;
            }
            if let Some(agent) = self.agent_dir(&entry, &by_name) {
                agents.insert(agent.id.clone(), agent);
            }
        }
        for agent in agents.values() {
            self.owner_assets(&agent.path, &Owner::Agent(agent.id.clone()));
        }
        let platform = self.root.join(PLATFORM);
        if platform.is_dir() {
            self.owner_assets(&platform, &Owner::Platform);
        }
        agents
    }

    /// 目录名（按 [`AgentName`] 比较：大小写、编码形式不同也算）对上数据库里的 agent，再读它的 agent.toml。
    fn agent_dir(&mut self, path: &Path, by_name: &BTreeMap<&AgentName, &AgentId>) -> Option<AgentDir> {
        let dir = file_name(path);
        let Some((name, id)) = AgentName::parse(&dir).ok().and_then(|n| by_name.get_key_value(&n)) else {
            self.problem(
                path.to_path_buf(),
                format!(
                    "no agent is named {dir:?}: agent directories are named after their agent, made by create_agent \
                     and renamed only in the app"
                ),
                None,
            );
            return None;
        };
        let file = path.join(AGENT_FILE);
        if !file.is_file() {
            self.problem(path.to_path_buf(), format!("no {AGENT_FILE}"), Some(id));
            return None;
        }
        match crate::formats::read(&file).and_then(|t| agent_file::parse(&t)) {
            Ok(data) => {
                Some(AgentDir { id: (*id).clone(), name: (*name).clone(), path: path.to_path_buf(), file: data })
            }
            Err(e) => {
                self.problem(file, e, Some(id));
                None
            }
        }
    }

    /// owner 目录里的每一项，按布局（[`place_of`]）认出是什么再读；配置文件另外读。
    fn owner_assets(&mut self, base: &Path, owner: &Owner) {
        match visible(base) {
            Ok(entries) => {
                for entry in entries {
                    self.visit(base, vec![file_name(&entry)], owner);
                }
            }
            Err(e) => self.problem(base.to_path_buf(), format!("cannot list: {e}"), owner.agent()),
        }
    }

    fn visit(&mut self, base: &Path, rel: Vec<String>, owner: &Owner) {
        let path = rel.iter().fold(base.to_path_buf(), |p, part| p.join(part));
        let place = match place_of(&rel, matches!(owner, Owner::Agent(_))) {
            Ok(p) => p,
            Err(e) => return self.problem(path, e.reason, owner.agent()),
        };
        match place {
            Place::Config => {}
            Place::Shelf(_) => {
                if !path.is_dir() {
                    return self.problem(path, "expected a directory", owner.agent());
                }
                match visible(&path) {
                    Ok(children) => {
                        for child in children {
                            let mut sub = rel.clone();
                            sub.push(file_name(&child));
                            self.visit(base, sub, owner);
                        }
                    }
                    Err(e) => self.problem(path, format!("cannot list: {e}"), owner.agent()),
                }
            }
            Place::PrivateMemory => {
                let id = owner.agent().expect("private memory is in an agent directory").clone();
                if path.is_dir() {
                    return self.problem(path, "expected a file", Some(&id));
                }
                // 显示的名字是 agent 名
                let name = self.known[&id].to_string();
                if let Some(asset) = self.loaded(StoredKind::Memory, &name, path, owner) {
                    self.private.insert(id, asset);
                }
            }
            Place::Entry { kind, name, .. } => {
                let directory = kind.spec().is_directory;
                if path.is_dir() != directory {
                    let what = if directory { "directory" } else { "file" };
                    return self.problem(path, format!("expected a {what}"), owner.agent());
                }
                match self.loaded(kind, &name, path, owner) {
                    Some(asset) => self.found.entry(asset.kind()).or_default().entry(name).or_default().push(asset),
                    None => {
                        self.unloaded.entry(kind).or_default().insert(name.clone());
                        if *owner == Owner::Platform {
                            self.broken_platform.entry(kind).or_default().insert(name);
                        }
                    }
                }
            }
        }
    }

    /// 按这种资源登记的读法读一项；读坏了记成问题、返回 None。
    fn loaded(&mut self, kind: StoredKind, name: &str, path: PathBuf, owner: &Owner) -> Option<Asset> {
        match (kind.spec().load)(&path, name, self.memory_max_chars) {
            Err(reason) => {
                self.problem(path, reason, owner.agent());
                None
            }
            Ok(got) => {
                if let Some(w) = got.warning {
                    self.problem(path.clone(), w, owner.agent());
                }
                Some(Asset {
                    name: name.to_owned(),
                    owner: owner.clone(),
                    path,
                    updated_at: got.updated_at,
                    content: got.content,
                })
            }
        }
    }

    fn rel(&self, path: &Path) -> String {
        path.strip_prefix(self.root).unwrap_or(path).display().to_string()
    }

    /// 同名的只在一处才加载。平台目录优先（#502）：平台的那份照常加载（读坏了也照样占着名字），agent 目录里同名的不
    /// 加载、报给那个 agent；agent 之间同名地位相同，全部不加载。
    fn resolved(&mut self, kind: StoredKind) -> BTreeMap<String, Asset> {
        let mut out = BTreeMap::new();
        let found = self.found.remove(&kind).unwrap_or_default();
        for (name, places) in found {
            let broken = self.broken_platform.get(&kind).is_some_and(|s| s.contains(&name));
            if places.len() == 1 && !broken {
                out.insert(name, places.into_iter().next().expect("one place"));
                continue;
            }
            let platform: Vec<&Asset> = places.iter().filter(|a| a.owner == Owner::Platform).collect();
            if platform.len() == 1 || broken {
                let where_ = match platform.first() {
                    Some(p) => self.rel(&p.path),
                    None => format!("{PLATFORM}/ (broken, see the problem reported for it)"),
                };
                let copies: Vec<&Asset> = places.iter().filter(|a| a.owner != Owner::Platform).collect();
                self.shadowed
                    .entry(kind)
                    .or_default()
                    .insert(name.clone(), copies.iter().map(|a| a.path.clone()).collect());
                let reports: Vec<(PathBuf, Option<AgentId>)> =
                    copies.iter().map(|a| (a.path.clone(), a.owner.agent().cloned())).collect();
                for (path, agent) in reports {
                    self.problem(
                        path,
                        format!(
                            "{} name {name:?} is already used by the platform's {where_}; the platform's one is loaded \
                             and this one is not: rename or remove it",
                            kind.label()
                        ),
                        agent.as_ref(),
                    );
                }
                if let Some(p) = platform.first() {
                    out.insert(name.clone(), (*p).clone());
                }
                continue;
            }
            for a in &places {
                let others: Vec<String> =
                    places.iter().filter(|p| p.path != a.path).map(|p| self.rel(&p.path)).collect();
                self.problem(
                    a.path.clone(),
                    format!(
                        "{} name {name:?} is also used by {others:?}; names are unique per kind, so none of them is \
                         loaded until all but one are renamed or removed",
                        kind.label()
                    ),
                    a.owner.agent(),
                );
            }
            self.unloaded.entry(kind).or_default().insert(name);
        }
        out
    }
}

/// 读整棵树。known 是数据库里的 agent（id → 名字，含正在删除的）：agent 目录按目录名对上它们；deleting 是
/// 其中正在删除的。last 是上一份快照（本进程还没读过为 None）：access.toml 整个读不出来时沿用它的角色与规则。同步 IO。
pub fn load_tree(
    root: &Path,
    known: &BTreeMap<AgentId, AgentName>,
    deleting: &BTreeSet<AgentId>,
    memory_max_chars: usize,
    last: Option<&Snapshot>,
    tools: Arc<ToolTable>,
) -> Snapshot {
    let mut r = Reader {
        root,
        known,
        memory_max_chars,
        problems: Vec::new(),
        found: BTreeMap::new(),
        unloaded: BTreeMap::new(),
        broken_platform: BTreeMap::new(),
        shadowed: BTreeMap::new(),
        private: BTreeMap::new(),
    };
    let agents = r.read_root();
    let reported: BTreeSet<Option<AgentId>> = r.problems.iter().map(|p| p.agent.clone()).collect();
    for (agent_id, name) in known {
        // 目录在、只是名字或内容不对的，上面已经报给它了
        if !agents.contains_key(agent_id) && !reported.contains(&Some(agent_id.clone())) {
            r.problem(
                root.join(name),
                format!(
                    "agent {name:?} ({agent_id}) has no directory named {name:?}; it cannot run until its directory \
                     is back"
                ),
                Some(agent_id),
            );
        }
    }
    let assets: BTreeMap<StoredKind, BTreeMap<String, Asset>> =
        StoredKind::ALL.into_iter().map(|k| (k, r.resolved(k))).collect();
    let path = root.join(PLATFORM).join(ACCESS_FILE);
    let loaded: BTreeSet<AgentId> = agents.keys().cloned().collect();
    let (roles, rules, access_text) = match access_file::read(&path, &loaded, &tools) {
        Access::Valid { roles, rules, problems, text } => {
            for p in problems {
                r.problem(path.clone(), p.reason, p.agent_id.as_ref());
            }
            (roles, rules, AccessText::Valid(text))
        }
        Access::Invalid(reasons) => {
            let (roles, rules, consequence) = match last {
                // 整个文件读不出来：沿用上一份快照的规则（它自己也可能是沿用来的），否则所有 agent 都没了 admin，平台的
                // 问题报不到人，也没有 agent 能改回这个文件
                Some(last) => (
                    last.roles.clone(),
                    last.rules.clone(),
                    "; the rules last read successfully stay in effect until it is fixed",
                ),
                None => (
                    access_file::builtin_roles(&tools),
                    BTreeMap::new(),
                    "; no earlier version was read since the service started, so only the fixed parts of built-in \
                     roles apply (no agent has admin)",
                ),
            };
            for reason in &reasons {
                r.problem(path.clone(), format!("{reason}{consequence}"), None);
            }
            (roles, rules, AccessText::Invalid(reasons.join("; ")))
        }
    };
    Snapshot {
        root: root.to_path_buf(),
        names: known.clone(),
        deleting: deleting.clone(),
        agents,
        assets,
        private: r.private,
        roles,
        rules,
        access_text,
        tools,
        problems: r.problems,
        unloaded: r.unloaded,
        shadowed: r.shadowed,
    }
}

/// 树的指纹：每个文件与目录的相对路径、修改时间、大小（隐藏的不算）。任何一个文件改了、增删、改名都会变；快照按它缓存。
pub fn fingerprint(root: &Path) -> Vec<(PathBuf, i128, u64)> {
    let mut out = Vec::new();
    walk(root, root, &mut out);
    out.sort();
    out
}

fn walk(root: &Path, dir: &Path, out: &mut Vec<(PathBuf, i128, u64)>) {
    let Ok(entries) = std::fs::read_dir(dir) else { return };
    for entry in entries.filter_map(|e| e.ok()) {
        if entry.file_name().to_string_lossy().starts_with('.') {
            continue;
        }
        let path = entry.path();
        // 跟随符号链接取时间与大小（同 os.stat），但不进链接到的目录（同 os.walk）
        let Ok(meta) = std::fs::metadata(&path) else { continue };
        let mtime = meta
            .modified()
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map_or(0, |d| d.as_nanos() as i128);
        out.push((path.strip_prefix(root).unwrap_or(&path).to_path_buf(), mtime, meta.len()));
        if entry.file_type().is_ok_and(|t| t.is_dir()) {
            walk(root, &path, out);
        }
    }
}

// ---- 平台自己写的辅助 ----

/// 一份资源文本的版本（摘要，换行统一成 `\n` 之后算）：打开编辑时随内容拿到，整体改写时交回（[`Expect::Version`]）。
pub fn version_of(text: &str) -> String {
    let digest = Sha256::digest(crate::formats::lf(text).as_bytes());
    digest.iter().take(8).map(|b| format!("{b:02x}")).collect()
}

/// 写一个资源文件之前对它此刻状态的预期（打开编辑、读到它时看到的）：对不上就是期间被别处改过、删掉或抢先建了，Conflict，
/// 不盖掉别人的改动。写入口（[`crate::source::AssetSource::write`]）在锁里、紧挨着写之前核对，各调用方只说预期什么。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Expect {
    /// 不管此刻是什么
    Any,
    /// 还不存在（新建）
    Absent,
    /// 内容是这个版本（[`version_of`]）
    Version(String),
    /// 修改时间是这个（读到它的那一刻）
    Modified(SystemTime),
}

impl Expect {
    /// 打开编辑时的版本，新建为 None。
    pub fn version(version: Option<&str>) -> Self {
        version.map_or(Expect::Absent, |v| Expect::Version(v.to_owned()))
    }

    /// 读到的内容，没有文件为 None。
    pub fn text(text: Option<&str>) -> Self {
        text.map_or(Expect::Absent, |t| Expect::Version(version_of(t)))
    }

    /// 此刻的 path 合不合预期，不合是 Conflict（说明是哪种不合、怎么办）。
    pub fn check(&self, path: &Path) -> Result<()> {
        let exists = std::fs::symlink_metadata(path).is_ok();
        let changed = || {
            conflict(format!(
                "{} was changed elsewhere after you opened or read it; reload it and redo your change on the current \
                 content. Nothing was written.",
                path.display()
            ))
        };
        match self {
            Expect::Any => Ok(()),
            Expect::Absent if !exists => Ok(()),
            Expect::Absent => Err(conflict(format!(
                "{} already exists: open or read it first, then change it. Nothing was written.",
                path.display()
            ))),
            _ if !exists => Err(conflict(format!(
                "{} was deleted after you opened or read it. Nothing was written.",
                path.display()
            ))),
            Expect::Version(v) => {
                let now = crate::formats::read(path).map_err(std::io::Error::other)?;
                if version_of(&now) == *v { Ok(()) } else { Err(changed()) }
            }
            Expect::Modified(at) => {
                let now = std::fs::metadata(path).and_then(|m| m.modified())?;
                if now == *at { Ok(()) } else { Err(changed()) }
            }
        }
    }
}

/// 删掉一个文件或目录（不存在不算错）。
pub fn remove(path: &Path) -> std::io::Result<()> {
    match std::fs::symlink_metadata(path) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
        Ok(m) if m.is_dir() => std::fs::remove_dir_all(path),
        Ok(_) => std::fs::remove_file(path),
    }
}

/// 改名或换 owner：目标已存在是冲突（不覆盖；大小写不敏感的文件系统上只改大小写的是同一个，照常改）。
pub fn move_to(src: &Path, dst: &Path) -> Result<()> {
    if dst.exists() && !same_file(src, dst) {
        return Err(muliao_platform::errors::invalid(format!("{} already exists", dst.display())));
    }
    if let Some(parent) = dst.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::rename(src, dst)?;
    Ok(())
}

fn same_file(a: &Path, b: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    match (std::fs::metadata(a), std::fs::metadata(b)) {
        (Ok(x), Ok(y)) => x.dev() == y.dev() && x.ino() == y.ino(),
        _ => false,
    }
}
