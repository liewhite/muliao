//! Skill：一个目录（`skills/<名>/`，在 owner 的资源目录下）：SKILL.md（YAML frontmatter：description，可选 name 须与目录
//! 同名，可选 approval）加任意附件。agent 直接用文件工具读、改、执行里面的文件；这里是平台自己的读写：skill 索引、
//! skill_run 要的脚本位置与批准、界面的编辑页（只改 SKILL.md）。

use muliao_platform::ids::AgentId;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use muliao_platform::fs::RealPath;
use sha2::{Digest, Sha256};

use crate::access::{Actor, Grants, grants_for, usable};
use crate::formats::skill::{SKILL_FILE, body_of, compose};
use crate::kinds::StoredKind;
use crate::source::AssetSource;
use crate::tree::{Asset, Expect, Owner, Snapshot, version_of};
use muliao_platform::errors::{Result, invalid, not_found};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillMeta {
    pub name: String,
    pub description: String,
    pub owner: Owner,
    /// SKILL.md 的位置
    pub path: PathBuf,
}

/// grants 能用的 skill，按名称。
pub(crate) fn index_of(snapshot: &Snapshot, grants: &Grants) -> Vec<SkillMeta> {
    usable(snapshot, StoredKind::Skill, grants).into_iter().map(meta).collect()
}

fn meta(a: &Asset) -> SkillMeta {
    SkillMeta {
        name: a.name.clone(),
        description: a.description().to_owned(),
        owner: a.owner.clone(),
        path: a.path.join(SKILL_FILE),
    }
}

#[derive(Debug, Clone)]
pub struct SkillScripts {
    /// skill 的目录（脚本在里面）
    pub directory: PathBuf,
    /// 执行前要用户批准的脚本（frontmatter 的 approval，读树时已换成文件的身份）
    pub approval: Vec<RealPath>,
}

impl SkillScripts {
    pub fn needs_approval(&self, script: &RealPath) -> bool {
        self.approval.contains(script)
    }

    /// skill 目录里除 SKILL.md 以外全部文件的内容指纹（sha256，十六进制）：「始终允许」按它认同一份脚本（#531），改了
    /// 脚本或它用的辅助文件就不再算同一份，改说明不算。按相对路径排序，路径与内容一起算；跳过 `__pycache__` 与隐藏的
    /// 文件和目录（脚本或系统自己写的，算进来跑一次就对不上）；符号链接跟进去算目标的内容，同一个目录只算一次。算法同
    /// Python 版，已有的「始终允许」照样认。阻塞 IO。
    pub fn fingerprint(&self) -> std::io::Result<String> {
        let mut digest = Sha256::new();
        let mut seen = Vec::new();
        fingerprint_dir(&self.directory, ".", &mut digest, &mut seen)?;
        Ok(format!("{:x}", digest.finalize()))
    }
}

fn fingerprint_dir(dir: &Path, rel: &str, digest: &mut Sha256, seen: &mut Vec<(u64, u64)>) -> std::io::Result<()> {
    // 读不了的目录整个跳过（同 os.walk：列不出的目录不出现）
    let Ok(entries) = std::fs::read_dir(dir) else { return Ok(()) };
    let st = std::fs::metadata(dir)?;
    if seen.contains(&(st.dev(), st.ino())) {
        return Ok(());
    }
    seen.push((st.dev(), st.ino()));
    digest.update(format!("{rel}/\0").as_bytes());
    let mut dirs = Vec::new();
    let mut files = Vec::new();
    for entry in entries.filter_map(|e| e.ok()) {
        let name = entry.file_name().to_string_lossy().into_owned();
        if std::fs::metadata(entry.path()).is_ok_and(|m| m.is_dir()) {
            if name != "__pycache__" && !name.starts_with('.') {
                dirs.push(name);
            }
        } else {
            files.push(name);
        }
    }
    files.sort();
    dirs.sort();
    let child = |name: &str| if rel == "." { name.to_owned() } else { format!("{rel}/{name}") };
    for name in files {
        let path = dir.join(&name);
        let file_rel = child(&name);
        if file_rel == SKILL_FILE || name.starts_with('.') || !path.is_file() {
            continue;
        }
        digest.update(format!("{file_rel}\0").as_bytes());
        digest.update(Sha256::digest(std::fs::read(&path)?));
    }
    for name in dirs {
        fingerprint_dir(&dir.join(&name), &child(&name), digest, seen)?;
    }
    Ok(())
}

#[derive(Debug, Clone)]
pub struct Skill {
    pub meta: SkillMeta,
    pub body: String,
    /// SKILL.md 的版本：整体改写时交回
    pub version: String,
}

pub struct SkillStore {
    source: Arc<AssetSource>,
}

impl SkillStore {
    pub fn new(source: Arc<AssetSource>) -> Self {
        Self { source }
    }

    /// agent 能用的这个 skill 的脚本在哪、哪些要批准（skill_run 用）；不存在或它不能用都是 NotFound。
    pub async fn scripts(&self, name: &str, agent_id: &AgentId) -> Result<SkillScripts> {
        let snapshot = self.source.snapshot().await?;
        let grants = grants_for(&snapshot, &Actor::Agent(agent_id.to_owned()));
        let found = usable(&snapshot, StoredKind::Skill, &grants).into_iter().find(|a| a.name == name);
        let a = found.ok_or_else(|| {
            not_found(format!("skill {name:?} not found among the skills you can use (see your skill index)"))
        })?;
        Ok(SkillScripts { directory: a.path.clone(), approval: a.content.skill().approval.clone() })
    }

    pub async fn read(&self, name: &str) -> Result<Skill> {
        let snapshot = self.source.snapshot().await?;
        let asset = snapshot.asset(StoredKind::Skill, name)?;
        let text = crate::formats::read(&asset.path.join(SKILL_FILE)).map_err(invalid)?;
        Ok(Skill { meta: meta(asset), body: body_of(&text), version: version_of(&text) })
    }

    /// skill 的目录（界面只读浏览它的文件用）。
    pub async fn directory(&self, name: &str) -> Result<PathBuf> {
        Ok(self.source.snapshot().await?.asset(StoredKind::Skill, name)?.path.clone())
    }

    /// 新建或整体改写 SKILL.md（附件不变；frontmatter 里别的键保留），用户在界面上编辑。已有的 owner 不变；新建的归
    /// owner。version 是打开编辑时 SKILL.md 的版本（新建为 None），期间被别处改过就 Conflict。
    pub async fn write(
        &self,
        name: &str,
        description: &str,
        body: &str,
        owner: &Owner,
        version: Option<&str>,
    ) -> Result<SkillMeta> {
        let snapshot = self.source.snapshot().await?;
        let (md, current) = match snapshot.existing(StoredKind::Skill, name)? {
            Some(a) => {
                let md = a.path.join(SKILL_FILE);
                let text = crate::formats::read(&md).map_err(invalid)?;
                (md, Some(text))
            }
            None => (snapshot.path_of(StoredKind::Skill, name, owner)?.join(SKILL_FILE), None),
        };
        let text = compose(current.as_deref(), name, description, body).map_err(invalid)?;
        self.source.write(&[(md, text, Expect::version(version))]).await?;
        Ok(meta(self.source.snapshot().await?.asset(StoredKind::Skill, name)?))
    }
}
