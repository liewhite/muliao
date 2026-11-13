//! 一个目录的只读浏览（界面 agent 页的「文件」与 skill 页的文件浏览）：列出目录里的一层、给出一个文件的位置与类型。
//!
//! 路径是相对这个目录的 posix 路径（空串是目录本身），解析符号链接后必须仍在目录内，否则拒绝；指向外面的符号链接也不列出。
//! 这只防止界面误走出去，不是安全隔离（agent 与服务同等可信，docs/architecture.md 第 15 节）。

use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use muliao_assets::service::workspace_of;
use muliao_files::INLINE_IMAGE_TYPES;
use muliao_platform::errors::DomainError;
use muliao_platform::fs::{RealPath, is_within, real, resolve_within};
use serde::Serialize;

use crate::file_tools::media_type_of;

fn is_inline(media_type: &str) -> bool {
    INLINE_IMAGE_TYPES.contains(&media_type)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Entry {
    pub name: String,
    pub is_dir: bool,
    /// 目录为 None
    pub size: Option<u64>,
    pub modified: DateTime<Utc>,
    /// 能内联显示的图片，与读文件时的响应一致
    pub inline: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkspaceFile {
    pub path: PathBuf,
    pub media_type: String,
}

impl WorkspaceFile {
    pub fn of(path: PathBuf) -> Self {
        let media_type = media_type_of(&path.file_name().unwrap_or_default().to_string_lossy());
        Self { path, media_type }
    }

    pub fn inline(&self) -> bool {
        is_inline(&self.media_type)
    }
}

/// root（已解析）下存在的 path。
fn target(root: &Path, path: &str) -> Result<RealPath, DomainError> {
    let target = resolve_within(root, path)?;
    if !target.as_path().exists() {
        return Err(DomainError::not_found(format!("no such file in the directory: {path:?}")));
    }
    Ok(target)
}

/// 目录 root 里 path（相对 root，空为 root 本身）这一层：目录在前，各自按名称排序。指向 root 外的符号链接、断掉或循环的
/// 符号链接等 stat 不了的条目不列出。阻塞 IO。
pub fn list_dir(root: &Path, path: &str) -> Result<Vec<Entry>, DomainError> {
    let root = real(root).map_err(|e| DomainError::not_found(format!("cannot open the directory: {e}")))?;
    let target = target(root.as_path(), path)?;
    if !target.as_path().is_dir() {
        return Err(DomainError::invalid(format!("not a directory: {path:?}")));
    }
    let read =
        std::fs::read_dir(target.as_path()).map_err(|e| DomainError::invalid(format!("cannot list {path:?}: {e}")))?;
    let mut entries = Vec::new();
    for child in read.filter_map(|c| c.ok()) {
        let child = child.path();
        if !real(&child).is_ok_and(|r| is_within(&root, &r)) {
            continue;
        }
        let Ok(st) = std::fs::metadata(&child) else { continue };
        let name = child.file_name().unwrap_or_default().to_string_lossy().into_owned();
        let is_dir = st.is_dir();
        entries.push(Entry {
            inline: !is_dir && is_inline(&media_type_of(&name)),
            name,
            is_dir,
            size: (!is_dir).then_some(st.len()),
            modified: st.modified().map(DateTime::<Utc>::from).unwrap_or_default(),
        });
    }
    entries.sort_by(|a, b| (!a.is_dir, &a.name).cmp(&(!b.is_dir, &b.name)));
    Ok(entries)
}

/// 目录 root 里的一个文件（path 相对 root）。阻塞 IO。
pub fn file_in(root: &Path, path: &str) -> Result<WorkspaceFile, DomainError> {
    let root = real(root).map_err(|e| DomainError::not_found(format!("cannot open the directory: {e}")))?;
    let target = target(root.as_path(), path)?;
    if !target.as_path().is_file() {
        return Err(DomainError::invalid(format!("not a file: {path:?}")));
    }
    Ok(WorkspaceFile::of(target.into_path_buf()))
}

/// agent 工作目录的浏览。
#[derive(Clone)]
pub struct WorkspaceFiles {
    workspaces_dir: PathBuf,
}

impl WorkspaceFiles {
    pub fn new(workspaces_dir: PathBuf) -> Self {
        Self { workspaces_dir }
    }

    fn root(&self, agent_id: &str) -> Result<PathBuf, DomainError> {
        let root = workspace_of(&self.workspaces_dir, agent_id);
        if !root.is_dir() {
            return Err(DomainError::not_found(format!("agent {agent_id:?} has no workspace")));
        }
        Ok(root)
    }

    pub fn list(&self, agent_id: &str, path: &str) -> Result<Vec<Entry>, DomainError> {
        list_dir(&self.root(agent_id)?, path)
    }

    pub fn file(&self, agent_id: &str, path: &str) -> Result<WorkspaceFile, DomainError> {
        file_in(&self.root(agent_id)?, path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lists_one_level_dirs_first_and_stays_inside() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("ws");
        std::fs::create_dir_all(root.join("sub")).unwrap();
        std::fs::write(root.join("b.png"), b"x").unwrap();
        std::fs::write(root.join("a.txt"), b"hello").unwrap();
        std::fs::write(dir.path().join("secret"), b"x").unwrap();
        std::os::unix::fs::symlink(dir.path().join("secret"), root.join("out")).unwrap();
        std::os::unix::fs::symlink(root.join("nowhere"), root.join("broken")).unwrap();
        let names: Vec<(String, bool, Option<u64>, bool)> =
            list_dir(&root, "").unwrap().into_iter().map(|e| (e.name, e.is_dir, e.size, e.inline)).collect();
        assert_eq!(
            names,
            [
                ("sub".into(), true, None, false),
                ("a.txt".into(), false, Some(5), false),
                ("b.png".into(), false, Some(1), true)
            ]
        );
        assert_eq!(file_in(&root, "a.txt").unwrap().media_type, "text/plain");
        assert!(file_in(&root, "../secret").is_err());
        assert!(file_in(&root, "out").is_err());
        assert_eq!(list_dir(&root, "nope").unwrap_err().message, "no such file in the directory: 'nope'");
        assert_eq!(file_in(&root, "sub").unwrap_err().message, "not a file: 'sub'");
    }
}
